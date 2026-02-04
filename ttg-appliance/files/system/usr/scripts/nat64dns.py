import asyncio
import ipaddress
import os
import netifaces
from typing import Optional
from dnslib import DNSRecord, DNSHeader, RR, AAAA, QTYPE, RCODE
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Configuration
INTERFACE_NAME = "tailscale0"
LISTEN_PORT = 53
NAT64_SUFFIX = "nat64"
BASE_PREFIX = "64:ff9b:1::"
NAT64_PREFIX_FILE = "/etc/tayga/default.conf"
UPSTREAM_DNS = "127.0.0.53"
UPSTREAM_PORT = 53


def get_interface_ipv6_addrs(iface_name):
    """
    Returns a list of IPv6 addresses assigned to the specific interface.
    """
    try:
        if iface_name not in netifaces.interfaces():
            print(f"Interface {iface_name} not found.")
            return []
        
        addrs = netifaces.ifaddresses(iface_name)
        # AF_INET6 is usually integer 10 or 30 depending on OS, netifaces handles this
        if netifaces.AF_INET6 in addrs:
            # Extract just the IP strings, removing scope IDs (e.g. %tailscale0) if present
            return [x['addr'].split('%')[0] for x in addrs[netifaces.AF_INET6]]
        return []
    except Exception as e:
        print(f"Error getting addresses for {iface_name}: {e}")
        return []


class UpstreamClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, query_data, future):
        self.query_data = query_data
        self.future = future
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport):
        self.transport = transport
        self.transport.sendto(self.query_data)

    # Renamed _addr -> addr to match asyncio.DatagramProtocol signature
    def datagram_received(self, data, addr):
        if not self.future.done():
            self.future.set_result(data)
        if self.transport:
            self.transport.close()

    def error_received(self, exc):
        if not self.future.done():
            self.future.set_exception(exc)

    def connection_lost(self, exc):
        if not self.future.done():
            if exc:
                self.future.set_exception(exc)
            else:
                self.future.set_exception(ConnectionError("Connection closed"))


async def query_upstream_async(query_data, upstream_ip, upstream_port):
    """
    Sends a raw DNS query to upstream asynchronously and awaits the response.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    try:
        # Attach a semaphore to the running loop to avoid cross-loop binding issues
        if not hasattr(loop, '_upstream_semaphore'):
            loop._upstream_semaphore = asyncio.Semaphore(64)

        await loop._upstream_semaphore.acquire()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: UpstreamClientProtocol(query_data, future),
                remote_addr=(upstream_ip, upstream_port),
            )

            # Wait for result with a timeout
            try:
                data = await asyncio.wait_for(future, timeout=2.0)
                return data
            except asyncio.TimeoutError:
                try:
                    transport.close()
                except Exception:
                    pass
                return None
            finally:
                try:
                    transport.close()
                except Exception:
                    pass
        finally:
            try:
                loop._upstream_semaphore.release()
            except Exception:
                pass
    except Exception as e:
        print(f"Async upstream query error: {e}")
        return None


async def load_nat64_prefix_async():
    """
    Reads the NAT64 prefix from the file system.
    Runs in a thread executor to avoid blocking the event loop with file I/O.
    """
    loop = asyncio.get_running_loop()

    # Use a small dedicated ThreadPoolExecutor to avoid unbounded thread creation
    global _FILE_IO_EXECUTOR, _NAT64_CACHE, _NAT64_CACHE_LOCK, _THREAD_FILE_SEMAPHORE

    def _read_and_parse():
        # Bound concurrent file reads using a threading semaphore inside executor
        acquired = _THREAD_FILE_SEMAPHORE.acquire(timeout=5)
        try:
            # Resolve symlinks safely
            real_path = os.path.realpath(NAT64_PREFIX_FILE)
            # Basic safety: ensure the prefix file is under /etc or same dir
            allowed_dir = os.path.realpath(os.path.dirname(NAT64_PREFIX_FILE))
            if not real_path.startswith(allowed_dir):
                # suspicious symlink, refuse to follow
                return None

            # stat may fail in tests where open is patched; allow proceeding
            try:
                st = os.stat(real_path)
                mtime = st.st_mtime
            except FileNotFoundError:
                mtime = None

            # Check cache under lock
            with _NAT64_CACHE_LOCK:
                cached = _NAT64_CACHE.get('net')
                cached_mtime = _NAT64_CACHE.get('mtime')
                cached_path = _NAT64_CACHE.get('path')
                if cached and cached_path == real_path and cached_mtime == mtime and mtime is not None:
                    return _NAT64_CACHE.get('raw')

            # Stream file to avoid big memory usage
            with open(real_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('prefix'):
                        parts = line.split()
                        if len(parts) >= 2:
                            prefix = parts[1]
                            # validate quickly before caching
                            try:
                                ipaddress.IPv6Network(prefix, strict=False)
                            except Exception:
                                return None
                            # update cache
                            with _NAT64_CACHE_LOCK:
                                _NAT64_CACHE['net'] = ipaddress.IPv6Network(prefix, strict=False)
                                _NAT64_CACHE['raw'] = prefix
                                _NAT64_CACHE['mtime'] = mtime
                                _NAT64_CACHE['path'] = real_path
                            return prefix

            return None
        except FileNotFoundError:
            return None
        except Exception as e:
            print(f"Error reading {NAT64_PREFIX_FILE}: {e}")
            return None
        finally:
            try:
                if acquired:
                    _THREAD_FILE_SEMAPHORE.release()
            except Exception:
                pass

    ipv6_prefix_str = await loop.run_in_executor(_FILE_IO_EXECUTOR, _read_and_parse)

    if not ipv6_prefix_str:
        return None

    try:
        # Validate and return IPv6Network
        net = ipaddress.IPv6Network(ipv6_prefix_str, strict=False)
        return net
    except ValueError:
        print(f"Invalid IPv6 prefix format: {ipv6_prefix_str}")
        return None

class NAT64Resolver:
    def resolve(self, qname_str):
        """
        Parses the query name and returns an ipaddress.IPv6Address or None.
        Sync function (CPU bound, fast enough to run in loop).
        """
        clean_qname = qname_str.lower().rstrip(".")
        if not clean_qname.endswith(NAT64_SUFFIX):
            return None

        prefix_len = len(NAT64_SUFFIX) + 1
        content = clean_qname[:-prefix_len]
        parts = content.split(".")

        if len(parts) < 2 or len(parts) > 3:
            return None

        ipv4_part_str = parts[0]
        # Flexible parsing: accept either 'ipv4.site.tcustomer' or 'ipv4.tcustomer.site'
        customer_id_str = parts[-1]
        site_id_str = "0"

        if len(parts) == 3:
            # detect which of the two middle/last parts carries the 't' prefix
            part1 = parts[1]
            part2 = parts[2]
            if part1.startswith('t'):
                customer_id_str = part1[1:]
                site_id_str = part2
            elif part2.startswith('t'):
                customer_id_str = part2[1:]
                site_id_str = part1
            else:
                # fallback to previous behavior: treat last part as customer, middle as site
                customer_id_str = parts[-1]
                site_id_str = parts[1]

        # allow an optional leading 't' on customer id when present
        if customer_id_str.startswith('t'):
            customer_id_str = customer_id_str[1:]

        try:
            customer_id = int(customer_id_str, 16)
            if customer_id > 0xFFFFFF:
                return None
        except ValueError:
            return None

        try:
            site_id = int(site_id_str, 16)
            if site_id > 0xFF:
                return None
        except ValueError:
            return None

        try:
            dotted_ipv4 = ipv4_part_str.replace("-", ".")
            ipv4_obj = ipaddress.IPv4Address(dotted_ipv4)
            ipv4_int = int(ipv4_obj)
        except (ValueError, ipaddress.AddressValueError):
            return None

        base_net = ipaddress.IPv6Network(BASE_PREFIX + "/96")
        base_int = int(base_net.network_address)

        constructed_suffix = (customer_id << 40) | (site_id << 32) | ipv4_int
        final_int = base_int | constructed_suffix

        return ipaddress.IPv6Address(final_int)


async def resolve_upstream_dns64(qname, nat64_net):
    """
    Queries upstream for A records and synthesizes AAAA records async.
    """
    try:
        # Create a query for A records
        upstream_q = DNSRecord.question(qname, "A")

        response_data = await query_upstream_async(
            upstream_q.pack(), UPSTREAM_DNS, UPSTREAM_PORT
        )

        if not response_data:
            return []

        upstream_reply = DNSRecord.parse(response_data)
        synthesized_ips = []
        prefix_int = int(nat64_net.network_address)

        # Determine how many host bits are available in the prefix
        host_bits = 128 - nat64_net.prefixlen
        if host_bits < 32:
            # Cannot represent a full IPv4 address in this prefix
            print(f"NAT64 prefix {nat64_net} too small to embed IPv4 (host_bits={host_bits})")
            return []

        for rr in upstream_reply.rr:
            if rr.rtype == QTYPE.A:
                ipv4_addr = ipaddress.IPv4Address(str(rr.rdata))
                ipv4_int = int(ipv4_addr) & 0xFFFFFFFF
                # IPv4 is placed in the low 32 bits; this works for /96 and any prefix with >=32 host bits
                synth_int = prefix_int | ipv4_int
                synthesized_ips.append(ipaddress.IPv6Address(synth_int))

        return synthesized_ips

    except Exception as e:
        print(f"Upstream resolution failed for {qname}: {e}")
        return []


# --- Module-level helpers and limits ---
# Thread pool for bounded file IO operations
_FILE_IO_EXECUTOR = ThreadPoolExecutor(max_workers=4)
# Cache for NAT64 prefix: {'net': IPv6Network, 'raw': str, 'mtime': float, 'path': str}
_NAT64_CACHE = {'net': None, 'raw': None, 'mtime': None, 'path': None}
_NAT64_CACHE_LOCK = threading.Lock()
# Threading semaphore to limit concurrent executor file reads (safe across event loops)
_THREAD_FILE_SEMAPHORE = threading.Semaphore(8)


class DNSServerProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Fire and forget task for each request to ensure concurrency
        asyncio.create_task(self.handle_request(data, addr))

    async def handle_request(self, data, addr):
        try:
            request = DNSRecord.parse(data)
            qname = str(request.q.qname)
            qtype = request.q.qtype

            reply = DNSRecord(
                DNSHeader(id=request.header.id, qr=1, aa=1, ra=1), q=request.q
            )

            resolver = NAT64Resolver()

            if qtype == QTYPE.AAAA:
                # 1. Custom Resolution
                result_ip = resolver.resolve(qname)

                if result_ip:
                    reply.add_answer(
                        RR(
                            rname=qname,
                            rtype=QTYPE.AAAA,
                            rclass=1,
                            ttl=300,
                            rdata=AAAA(str(result_ip)),
                        )
                    )
                    print(f"Query: {qname} [Custom] -> {result_ip}")
                else:
                    # 2. Fallback DNS64 Synthesis
                    nat64_net = await load_nat64_prefix_async()

                    if nat64_net:
                        synthesized_ips = await resolve_upstream_dns64(
                            qname, nat64_net
                        )
                        if synthesized_ips:
                            for ip in synthesized_ips:
                                reply.add_answer(
                                    RR(
                                        rname=qname,
                                        rtype=QTYPE.AAAA,
                                        rclass=1,
                                        ttl=60,
                                        rdata=AAAA(str(ip)),
                                    )
                                )
                            print(
                                f"Query: {qname} [DNS64] -> {len(synthesized_ips)} records"
                            )
                        else:
                            reply.header.rcode = RCODE.NOERROR
                            print(
                                f"Query: {qname} [DNS64] -> Empty or Upstream Fail"
                            )
                    else:
                        reply.header.rcode = RCODE.NOERROR
                        print(f"Query: {qname} [Failed] -> No Prefix File")
            else:
                reply.header.rcode = RCODE.NOERROR

            self.transport.sendto(reply.pack(), addr)

        except Exception as e:
            print(f"Error processing request from {addr}: {e}")

async def main():
    loop = asyncio.get_running_loop()

    initial_check = await load_nat64_prefix_async()
    if not initial_check:
        print(f"WARNING: {NAT64_PREFIX_FILE} not found or invalid. Fallback DNS64 will not work.")

    # Get IPs specifically for tailscale0
    listen_ips = get_interface_ipv6_addrs(INTERFACE_NAME)
    
    if not listen_ips:
        print(f"No IPv6 addresses found on {INTERFACE_NAME}. Exiting.")
        return

    transports = []

    try:
        # Create a server endpoint for every IP found on the interface
        for ip in listen_ips:
            print(f"Binding to {INTERFACE_NAME} -> [{ip}]:{LISTEN_PORT}")
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: DNSServerProtocol(),
                    local_addr=(ip, LISTEN_PORT)
                )
                transports.append(transport)
            except OSError as e:
                print(f"Failed to bind {ip}: {e}")

        if not transports:
            print("Could not bind to any addresses.")
            return

        print("DNS Server is running.")
        
        try:
            await asyncio.Future() # Run forever
        finally:
            for t in transports:
                t.close()

    except PermissionError:
        print(f"Permission denied. Try running with sudo/admin privileges to bind to port {LISTEN_PORT}.")
    except Exception as e:
        print(f"Fatal error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
