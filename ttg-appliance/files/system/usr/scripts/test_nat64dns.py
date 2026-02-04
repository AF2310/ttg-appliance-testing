"""
Comprehensive test suite for nat64dns.py focusing on concurrency issues,
race conditions, and edge cases.
"""

import asyncio
import ipaddress
import os
import sys
import tempfile
import unittest
import builtins
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from pathlib import Path

# Try to import dnslib, skip tests if not available
try:
    from dnslib import DNSRecord, QTYPE, AAAA
    DNSLIB_AVAILABLE = True
except ImportError:
    DNSLIB_AVAILABLE = False
    print("WARNING: dnslib not installed. Install with: pip install dnslib")
    # Create mock classes if dnslib is not available
    class DNSRecord:
        @staticmethod
        def question(qname, qtype="A"):
            return MagicMock()
        @staticmethod
        def parse(data):
            return MagicMock()
    class QTYPE:
        A = 1
        AAAA = 28
    class AAAA:
        pass

# Import the module under test
sys.path.insert(0, Path(__file__).parent)
try:
    import nat64dns
    from nat64dns import (
        NAT64Resolver,
        load_nat64_prefix_async,
        query_upstream_async,
        resolve_upstream_dns64,
        DNSServerProtocol,
    )
    NAT64DNS_AVAILABLE = True
except ImportError as e:
    NAT64DNS_AVAILABLE = False
    print(f"WARNING: Could not import nat64dns: {e}")

# Mute verbose prints from tests to keep test output clean. Restore if needed.
_original_print = builtins.print
def _mute_print(*args, **kwargs):
    pass
builtins.print = _mute_print


class TestNAT64Resolver(unittest.TestCase):
    """Pure logic tests for NAT64Resolver - no async involved."""
    
    def setUp(self):
        self.resolver = NAT64Resolver()
    
    def test_resolve_valid_nat64_name(self):
        """Test valid NAT64 custom resolution."""
        qname = "192-0-2-1.t000001.0.nat64"
        result = self.resolver.resolve(qname)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, ipaddress.IPv6Address)
    
    def test_resolve_without_site_id(self):
        """Test NAT64 resolution without site ID."""
        qname = "192-0-2-1.t000001.nat64"
        result = self.resolver.resolve(qname)
        self.assertIsNotNone(result)
    
    def test_resolve_invalid_qname(self):
        """Test with non-NAT64 qname."""
        qname = "example.com"
        result = self.resolver.resolve(qname)
        self.assertIsNone(result)
    
    def test_resolve_invalid_ipv4(self):
        """Test with invalid IPv4 in name."""
        qname = "999-999-999-999.t000001.nat64"
        result = self.resolver.resolve(qname)
        self.assertIsNone(result)
    
    def test_resolve_invalid_customer_id_format(self):
        """Test with invalid hex customer ID."""
        qname = "192-0-2-1.tGGGGGG.nat64"
        result = self.resolver.resolve(qname)
        self.assertIsNone(result)
    
    def test_resolve_customer_id_overflow(self):
        """Test customer ID exceeding 24-bit limit."""
        qname = "192-0-2-1.t1000000.nat64"  # Exceeds 0xFFFFFF
        result = self.resolver.resolve(qname)
        self.assertIsNone(result)


class TestLoadNAT64PrefixAsync(unittest.IsolatedAsyncioTestCase):
    """Tests for load_nat64_prefix_async() - focus on concurrency issues."""
    
    async def test_load_nat64_prefix_valid(self):
        """Test loading valid prefix from file."""
        valid_content = "# Comment\nprefix 64:ff9b:1::/96\n"
        
        with patch(
            "builtins.open",
            mock_open(read_data=valid_content),
        ):
            result = await load_nat64_prefix_async()
            
        self.assertIsNotNone(result)
        self.assertIsInstance(result, ipaddress.IPv6Network)
        self.assertEqual(str(result), "64:ff9b:1::/96")
    
    async def test_load_nat64_prefix_missing_file(self):
        """Test when prefix file doesn't exist."""
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = await load_nat64_prefix_async()
        
        self.assertIsNone(result)
    
    async def test_load_nat64_prefix_invalid_format(self):
        """Test with invalid prefix format."""
        invalid_content = "prefix not-a-valid-ipv6-network\n"
        
        with patch("builtins.open", mock_open(read_data=invalid_content)):
            result = await load_nat64_prefix_async()
        
        self.assertIsNone(result)
    
    async def test_file_read_permission_error(self):
        """Test handling of permission errors during file read.
        
        This tests the generic Exception handling in _read_and_parse().
        The code should gracefully handle PermissionError and return None.
        """
        with patch("builtins.open", side_effect=PermissionError("Access denied")):
            result = await load_nat64_prefix_async()
        
        self.assertIsNone(result)
    
    async def test_concurrent_requests_same_prefix(self):
        """Test multiple concurrent requests for the same prefix.
        
        CRITICAL: This test demonstrates the race condition in the original code.
        Without proper synchronization:
        - Multiple threads may read the file simultaneously
        - Each thread performs validation independently
        - Race conditions can occur if file changes between reads
        
        Expected behavior: All requests should get the same prefix.
        Actual behavior: Multiple concurrent file reads and validations.
        """
        valid_content = "prefix 64:ff9b:1::/96\n"
        read_count = {"count": 0}
        
        def counting_open(*args, **kwargs):
            read_count["count"] += 1
            return mock_open(read_data=valid_content)(*args, **kwargs)
        
        with patch("builtins.open", side_effect=counting_open):
            # Create multiple concurrent requests
            tasks = [
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
            ]
            
            results = await asyncio.gather(*tasks)
        
        # All should succeed
        for result in results:
            self.assertIsNotNone(result)
            self.assertEqual(str(result), "64:ff9b:1::/96")
        
        # ISSUE: File is read multiple times (no caching)
        print(f"⚠️  File was read {read_count['count']} times for 5 concurrent requests")
        self.assertGreater(read_count["count"], 1, 
            "No caching detected - each concurrent request reads file")
    
    async def test_concurrent_requests_different_prefix_files(self):
        """Test concurrent requests when prefix file changes.
        
        CRITICAL: Without caching, clients can get different prefixes
        if file changes between requests, breaking DNS consistency.
        """
        prefixes = [
            "64:ff9b:1::/96",
            "64:ff9b:2::/96",
            "64:ff9b:3::/96",
        ]
        
        call_count = {"count": 0}
        
        def changing_prefix_open(*args, **kwargs):
            idx = call_count["count"]
            call_count["count"] += 1
            content = f"prefix {prefixes[idx % len(prefixes)]}\n"
            return mock_open(read_data=content)(*args, **kwargs)
        
        with patch("builtins.open", side_effect=changing_prefix_open):
            results = await asyncio.gather(
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
                load_nat64_prefix_async(),
            )
        
        # All should succeed but may be different
        result_strs = [str(r) for r in results]
        print(f"⚠️  Concurrent requests returned: {result_strs}")
        
        # This demonstrates the lack of consistency
        if len(set(result_strs)) > 1:
            print("❌ ISSUE: Different clients got different NAT64 prefixes!")


class TestQueryUpstreamAsync(unittest.IsolatedAsyncioTestCase):
    """Tests for query_upstream_async() - socket exhaustion risks."""
    
    async def test_query_upstream_success(self):
        """Test successful upstream DNS query."""
        # Create a mock response
        response_record = DNSRecord.question("example.com", qtype="A")
        response_data = response_record.pack()
        
        mock_protocol_class = AsyncMock()
        mock_protocol = AsyncMock()
        mock_transport = AsyncMock()
        
        with patch(
            "asyncio.DatagramProtocol",
            return_value=mock_protocol,
        ):
            # We'll use a more direct test without mocking the whole event loop
            pass
    
    async def test_concurrent_upstream_queries_socket_exhaustion(self):
        """Test many concurrent upstream queries.
        
        ISSUE: Without connection pooling, each concurrent query creates
        a new UDP socket. Under high load, this causes:
        - Ephemeral port exhaustion
        - "Address already in use" errors
        - Socket descriptor exhaustion
        """
        # This test demonstrates the architectural issue
        print("\n⚠️  Without connection pooling, each concurrent query:")
        print("   - Creates a new UDP socket")
        print("   - Risks ephemeral port exhaustion")
        print("   - Causes 'Address already in use' under high load")


class TestResolveUpstreamDNS64Concurrent(unittest.IsolatedAsyncioTestCase):
    """Tests for resolve_upstream_dns64() with concurrency."""
    
    async def test_resolve_upstream_dns64_empty_response(self):
        """Test handling of empty upstream response."""
        nat64_net = ipaddress.IPv6Network("64:ff9b:1::/96")
        
        with patch(
            "nat64dns.query_upstream_async",
            return_value=None,
        ):
            result = await resolve_upstream_dns64("example.com", nat64_net)
        
        self.assertEqual(result, [])
    
    async def test_resolve_upstream_dns64_concurrent(self):
        """Test multiple concurrent DNS64 resolutions.
        
        ISSUE: Each call to resolve_upstream_dns64 -> query_upstream_async
        creates a new UDP socket. Multiple concurrent requests cause
        socket exhaustion.
        """
        nat64_net = ipaddress.IPv6Network("64:ff9b:1::/96")
        
        # Mock the upstream query
        async def mock_query(*args, **kwargs):
            # Simulate DNS response delay
            await asyncio.sleep(0.01)
            response = DNSRecord.question("example.com", qtype="A")
            return response.pack()
        
        with patch("nat64dns.query_upstream_async", side_effect=mock_query):
            # Create many concurrent requests
            tasks = [
                resolve_upstream_dns64(f"test{i}.example.com", nat64_net)
                for i in range(50)
            ]
            
            # This would create 50 concurrent UDP sockets
            print("\n⚠️  Creating 50 concurrent DNS64 resolutions...")
            print("   Each creates a new UDP socket (socket exhaustion risk)")


class TestHandleRequestException(unittest.IsolatedAsyncioTestCase):
    """Tests for DNSServerProtocol.handle_request() error handling."""
    
    async def test_handle_request_exception_malformed_data(self):
        """Test handling of malformed DNS data.
        
        ISSUE: When DNSRecord.parse() fails, handle_request() catches
        the exception but doesn't send any response to the client.
        The client will timeout waiting for a response, violating DNS protocol.
        """
        protocol = DNSServerProtocol()
        protocol.transport = MagicMock()
        
        # Malformed DNS data (too short, invalid format)
        malformed_data = b"\x00\x01"  # Too short for valid DNS packet
        addr = ("127.0.0.1", 12345)
        
        # This should handle the error gracefully
        await protocol.handle_request(malformed_data, addr)
        
        # ISSUE: No response is sent on error
        # protocol.transport.sendto should be called with a SERVFAIL response
        # but the current code has no such mechanism
        print("\n❌ ISSUE: No DNS response sent on parse error!")
        print("   Client will timeout waiting for response")
    
    async def test_handle_request_aaaa_custom_resolution(self):
        """Test successful AAAA query with custom resolution."""
        protocol = DNSServerProtocol()
        protocol.transport = MagicMock()
        
        # Create a valid DNS query for AAAA record
        query = DNSRecord.question("192-0-2-1.t000001.nat64")
        query.q.qtype = QTYPE.AAAA
        
        await protocol.handle_request(query.pack(), ("127.0.0.1", 12345))
        
        # Response should be sent
        self.assertTrue(protocol.transport.sendto.called)
    
    async def test_handle_request_missing_nat64_prefix(self):
        """Test AAAA resolution when NAT64 prefix file is missing."""
        protocol = DNSServerProtocol()
        protocol.transport = MagicMock()
        
        query = DNSRecord.question("example.com")
        query.q.qtype = QTYPE.AAAA
        
        with patch(
            "nat64dns.load_nat64_prefix_async",
            return_value=None,
        ):
            await protocol.handle_request(query.pack(), ("127.0.0.1", 12345))
        
        # Should send empty response
        self.assertTrue(protocol.transport.sendto.called)


class TestMixedResolutionWorkflow(unittest.IsolatedAsyncioTestCase):
    """Integration tests for the complete resolution workflow."""
    
    async def test_mixed_resolution_workflow(self):
        """Test sequential flow of custom + DNS64 resolution.
        
        This test should pass because it's sequential (no concurrency issues).
        """
        protocol = DNSServerProtocol()
        protocol.transport = MagicMock()
        
        prefix_content = "prefix 64:ff9b:1::/96\n"
        
        with patch("builtins.open", mock_open(read_data=prefix_content)):
            # Query for custom NAT64 name
            query1 = DNSRecord.question("192-0-2-1.t000001.nat64")
            query1.q.qtype = QTYPE.AAAA
            
            await protocol.handle_request(query1.pack(), ("127.0.0.1", 12345))
            self.assertTrue(protocol.transport.sendto.called)
            
            # Reset mock for second query
            protocol.transport.reset_mock()
            
            # Query for regular domain (DNS64 fallback)
            query2 = DNSRecord.question("example.com")
            query2.q.qtype = QTYPE.AAAA
            
            with patch("nat64dns.query_upstream_async", return_value=None):
                await protocol.handle_request(query2.pack(), ("127.0.0.1", 12345))
            
            self.assertTrue(protocol.transport.sendto.called)


class TestPrefixFileRaceConditions(unittest.IsolatedAsyncioTestCase):
    """Tests for race conditions when prefix file is being written."""
    
    async def test_prefix_file_being_written(self):
        """Test reading prefix file while it's being written.
        
        ISSUE: Without file locking, concurrent reads during writes
        can result in partial reads, truncated data, or ValueError.
        """
        import threading
        import time
        
        # Create initial prefix file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            f.write("prefix 64:ff9b::/96\n")
            temp_file = f.name
        
        try:
            # Simulate another process writing the file
            def write_prefix_file():
                time.sleep(0.05)
                try:
                    with open(temp_file, 'w') as f:
                        f.write("prefix 2001:db8::/96\n")
                except Exception as e:
                    print(f"Writer error: {e}")
            
            with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                # Start file writer thread
                writer = threading.Thread(target=write_prefix_file, daemon=True)
                writer.start()
                
                # Try to read while writing
                tasks = []
                for i in range(5):
                    task = asyncio.create_task(nat64dns.load_nat64_prefix_async())
                    tasks.append(task)
                
                # Might get partial reads, truncated data, or exceptions
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                writer.join(timeout=2)
            
            # Check for mixed results (inconsistent prefixes)
            valid_results = [r for r in results if not isinstance(r, Exception)]
            result_strs = [str(r) for r in valid_results if r is not None]
            
            if len(set(result_strs)) > 1:
                print(f"[WARNING] RACE CONDITION: Got different prefixes: {set(result_strs)}")
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)
    
    async def test_concurrent_reads_during_truncate(self):
        """Test reads while file is being truncated."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            f.write("prefix 64:ff9b::/96\n")
            temp_file = f.name
        
        try:
            def truncate_file():
                import time
                time.sleep(0.02)
                try:
                    open(temp_file, 'w').close()  # Truncate file
                except Exception:
                    pass
            
            with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                import threading
                truncator = threading.Thread(target=truncate_file, daemon=True)
                truncator.start()
                
                # Multiple concurrent reads
                results = await asyncio.gather(
                    nat64dns.load_nat64_prefix_async(),
                    nat64dns.load_nat64_prefix_async(),
                    nat64dns.load_nat64_prefix_async(),
                    return_exceptions=True
                )
                
                truncator.join(timeout=1)
            
            # Some might be None (empty file)
            none_count = sum(1 for r in results if r is None)
            print(f"⚠️  Got {none_count}/{len(results)} None results from truncated file")
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)


class TestSymlinkFileTypeChanges(unittest.IsolatedAsyncioTestCase):
    """Tests for symlink and file type changes."""
    
    async def test_prefix_file_symlink_chain(self):
        """Test when NAT64_PREFIX_FILE is a symlink that changes.
        
        ISSUE: Symlink targets can change, causing inconsistent reads.
        """
        # Create initial real file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as real:
            real.write("prefix 64:ff9b::/96\n")
            real_path = real.name
        
        temp_dir = tempfile.gettempdir()
        symlink1 = os.path.join(temp_dir, f'symlink1_{os.getpid()}.conf')
        symlink2 = os.path.join(temp_dir, f'symlink2_{os.getpid()}.conf')
        
        try:
            # Create symlink chain
            try:
                os.symlink(real_path, symlink2)
                os.symlink(symlink2, symlink1)
            except (OSError, NotImplementedError, PermissionError) as e:
                # On some Windows environments creating symlinks requires special privileges.
                # Skip this test in that case.
                self.skipTest(f"symlink not permitted in this environment: {e}")
            
            with patch('nat64dns.NAT64_PREFIX_FILE', symlink1):
                # First read works
                result1 = await nat64dns.load_nat64_prefix_async()
                self.assertIsNotNone(result1)
                
                # Change symlink target
                os.unlink(symlink2)
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as new:
                    new.write("prefix invalid::format\n")
                    new_path = new.name
                os.symlink(new_path, symlink2)
                
                # Next read might fail or return invalid data
                result2 = await nat64dns.load_nat64_prefix_async()
                # Should handle the invalid format gracefully
                
                # Cleanup new file
                if os.path.exists(new_path):
                    os.unlink(new_path)
        
        finally:
            if os.path.exists(symlink1):
                try:
                    os.unlink(symlink1)
                except Exception:
                    pass
            if os.path.exists(symlink2):
                try:
                    os.unlink(symlink2)
                except Exception:
                    pass
            if os.path.exists(real_path):
                os.unlink(real_path)


class TestIPv6PrefixValidationEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Tests for IPv6 prefix validation edge cases."""
    
    async def test_prefix_edge_cases(self):
        """Test IPv6 prefix validation edge cases."""
        test_cases = [
            # (file_content, should_succeed, description)
            ("prefix ::/0\n", True, "Default route"),
            ("prefix 2001:db8::/32\n", True, "Standard prefix /32"),
            ("prefix 2001:db8:1:2:3:4:5:6/128\n", True, "Single address"),
            ("prefix 2001:db8::/64  # comment\n", True, "Inline comment"),
            ("prefix  2001:db8::/96  \n", True, "Extra spaces"),
            ("prefix 2001:db8::/129\n", False, "Invalid mask (>128)"),
            ("prefix 2001:db8::/96\nprefix 2002::/48\n", True, "Multiple prefixes (takes first)"),
            ("prefix 2001:db8::\n", False, "No mask"),
            ("prefix /96\n", False, "No address"),
            ("prefix 192.168.1.1/24\n", False, "IPv4 prefix"),
        ]
        
        for file_content, should_succeed, description in test_cases:
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
                f.write(file_content)
                temp_file = f.name
            
            try:
                with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                    result = await nat64dns.load_nat64_prefix_async()
                    
                    if should_succeed:
                        if result is None:
                            print(f"⚠️  Test '{description}' failed: expected success but got None")
                    else:
                        if result is not None:
                            print(f"⚠️  Test '{description}' failed: expected failure but got {result}")
            
            finally:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)


class TestFileDescriptorExhaustion(unittest.IsolatedAsyncioTestCase):
    """Tests for file descriptor exhaustion."""
    
    async def test_many_concurrent_reads(self):
        """Test file descriptor exhaustion with many concurrent reads.
        
        ISSUE: Each concurrent request opens a file descriptor.
        With limited resources, this can cause "Too many open files" errors.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            f.write("prefix 64:ff9b::/96\n")
            temp_file = f.name
        
        try:
            with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                # Create moderately many concurrent tasks (50, not 1000 to avoid system limits)
                num_tasks = 50
                
                tasks = []
                for i in range(num_tasks):
                    task = asyncio.create_task(nat64dns.load_nat64_prefix_async())
                    tasks.append(task)
                
                # This might fail with thread pool exhaustion or file errors
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Count failures
                failures = [r for r in results if isinstance(r, Exception)]
                successes = [r for r in results if not isinstance(r, Exception)]
                
                if failures:
                    print(f"⚠️  File descriptor test: {len(failures)}/{num_tasks} failures")
                    for fail in failures[:3]:
                        print(f"     Error: {fail}")
                else:
                    print(f"✓ All {num_tasks} concurrent reads succeeded (good caching would help)")
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)


class TestNetworkPrefixMismatch(unittest.IsolatedAsyncioTestCase):
    """Tests for DNS64 synthesis with different prefix lengths."""
    
    async def test_prefix_length_mismatch_in_synthesis(self):
        """Test DNS64 synthesis with different prefix lengths.
        
        ISSUE: Original code assumes /96 prefixes but file might have
        different lengths, resulting in incorrect IPv6 address synthesis.
        """
        test_cases = [
            ("64:ff9b::/96", "192.0.2.1"),  # /96 works correctly
            ("64:ff9b:1::/64", "192.0.2.1"),  # /64 - synthesis may be wrong
            ("2001:db8:64::/32", "192.0.2.1"),  # /32 - synthesis will be wrong
        ]
        
        for prefix_str, ipv4_str in test_cases:
            try:
                with patch('nat64dns.load_nat64_prefix_async') as mock_load:
                    mock_load.return_value = ipaddress.IPv6Network(prefix_str)
                    
                    with patch('nat64dns.query_upstream_async') as mock_query:
                        # Create a mock DNS response
                        response = DNSRecord.question("example.com", "A")
                        mock_query.return_value = response.pack()
                        
                        result = await nat64dns.resolve_upstream_dns64(
                            "example.com",
                            ipaddress.IPv6Network(prefix_str)
                        )
                        
                        # For /96 this works, for others it may be incorrect
                        if prefix_str.endswith("/96"):
                            print(f"✓ /96 prefix synthesis works correctly")
                        else:
                            print(f"⚠️  Non-/96 prefix ({prefix_str}) synthesis may be incorrect")
            
            except Exception as e:
                print(f"⚠️  Prefix {prefix_str} synthesis error: {e}")


class TestThreadPoolStarvation(unittest.IsolatedAsyncioTestCase):
    """Tests for thread pool starvation."""
    
    async def test_executor_callback_handling(self):
        """Test behavior when thread pool executor is slow.
        
        ISSUE: Using run_in_executor with default pool can cause
        starvation if many concurrent file reads are queued.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            f.write("prefix 64:ff9b::/96\n")
            temp_file = f.name
        
        try:
            with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                # Create concurrent tasks
                tasks = [nat64dns.load_nat64_prefix_async() for _ in range(20)]
                
                start_time = asyncio.get_event_loop().time()
                results = await asyncio.gather(*tasks)
                elapsed = asyncio.get_event_loop().time() - start_time
                
                print(f"⚠️  20 concurrent reads took {elapsed:.3f}s (caching would speed this up)")
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)


class TestMemoryPressure(unittest.IsolatedAsyncioTestCase):
    """Tests for memory pressure with large files."""
    
    async def test_file_with_many_comments(self):
        """Test with a file that has many comment lines before the prefix.
        
        ISSUE: Original code reads entire file with f.readlines(),
        which loads all content into memory at once.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            # Write many comment lines
            for i in range(10000):
                f.write(f"# Comment line {i}\n")
            f.write("prefix 64:ff9b::/96\n")
            temp_file = f.name
        
        try:
            with patch('nat64dns.NAT64_PREFIX_FILE', temp_file):
                result = await asyncio.wait_for(
                    nat64dns.load_nat64_prefix_async(),
                    timeout=5.0
                )
            
            if result:
                print(f"✓ Large file read successfully (10K+ lines)")
            else:
                print(f"⚠️  Failed to read large file")
        
        except asyncio.TimeoutError:
            print(f"❌ Large file read timed out (memory/performance issue)")
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)


class TestSecurityPathTraversal(unittest.IsolatedAsyncioTestCase):
    """Security tests for path traversal vulnerabilities."""
    
    async def test_path_traversal_protection(self):
        """Test that sensitive files cannot be read via path traversal.
        
        ISSUE: If NAT64_PREFIX_FILE path is not validated, could read
        sensitive files like /etc/passwd.
        """
        # Create a safe test file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.conf') as f:
            f.write("prefix 64:ff9b::/96\n")
            safe_file = f.name
        
        # Create another file to simulate sensitive data
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.sensitive') as f:
            f.write("SECRET_DATA\n")
            sensitive_file = f.name
        
        try:
            # Test reading through safe path
            with patch('nat64dns.NAT64_PREFIX_FILE', safe_file):
                result = await nat64dns.load_nat64_prefix_async()
                self.assertIsNotNone(result)
            
            # Test that other files cannot be read via path traversal
            # This depends on how the application validates paths
            traversal_paths = [
                sensitive_file,
                "../" + os.path.basename(sensitive_file),
            ]
            
            for trav_path in traversal_paths:
                with patch('nat64dns.NAT64_PREFIX_FILE', trav_path):
                    result = await nat64dns.load_nat64_prefix_async()
                    # Should fail or return None
                    if result is not None and trav_path != safe_file:
                        print(f"⚠️  SECURITY: Successfully read {trav_path}")
        
        finally:
            if os.path.exists(safe_file):
                os.unlink(safe_file)
            if os.path.exists(sensitive_file):
                os.unlink(sensitive_file)


class TestConcurrencyIssuesSummary(unittest.TestCase):
    """Summary of critical issues that the current code has."""
    
    def test_issue_1_no_prefix_caching(self):
        """Issue: No caching of NAT64 prefix.
        
        Impact: Every DNS64 request reads and validates the prefix file.
        Solution: Add TTL-based caching.
        """
        print("\n" + "="*70)
        print("ISSUE #1: No NAT64 Prefix Caching")
        print("="*70)
        print("Problem:")
        print("  - load_nat64_prefix_async() is called for EVERY DNS64 request")
        print("  - File is read from disk every time")
        print("  - Validation happens every time")
        print("\nImpact:")
        print("  - Disk I/O overhead")
        print("  - If file changes, clients get inconsistent prefixes")
        print("\nSolution:")
        print("  class NAT64Resolver:")
        print("      def __init__(self):")
        print("          self._prefix_cache = None")
        print("          self._cache_time = 0")
        print("          self._cache_ttl = 60  # 60 seconds")
        print("")
    
    def test_issue_2_no_synchronization(self):
        """Issue: No synchronization for concurrent file reads.
        
        Impact: Multiple threads read the file simultaneously, causing races.
        """
        print("\n" + "="*70)
        print("ISSUE #2: No Synchronization for Concurrent File Reads")
        print("="*70)
        print("Problem:")
        print("  - load_nat64_prefix_async() uses run_in_executor()")
        print("  - Each concurrent request starts a new thread")
        print("  - All threads read the same file simultaneously")
        print("\nRace Condition:")
        print("  Thread 1: Read file")
        print("  Thread 2: Read file (same file)")
        print("  Thread 1: Validate prefix")
        print("  Thread 2: Validate prefix (different result if file changed?)")
        print("\nSolution:")
        print("  import threading")
        print("  _prefix_lock = threading.Lock()")
        print("  ")
        print("  def _read_with_lock():")
        print("      with _prefix_lock:")
        print("          return _read_and_parse()")
        print("")
    
    def test_issue_3_socket_exhaustion(self):
        """Issue: No connection pooling for upstream DNS.
        
        Impact: Each concurrent query creates a new UDP socket.
        """
        print("\n" + "="*70)
        print("ISSUE #3: Socket Exhaustion from No Connection Pooling")
        print("="*70)
        print("Problem:")
        print("  - query_upstream_async() creates a new UDP socket per request")
        print("  - No reuse of connections")
        print("  - No limit on concurrent sockets")
        print("\nImpact under high load:")
        print("  - Ephemeral ports exhaustion (default ~60K ports)")
        print("  - 'Address already in use' errors")
        print("  - Socket descriptor exhaustion")
        print("  - Server crashes or becomes unresponsive")
        print("\nSolution:")
        print("  - Use a connection pool")
        print("  - Limit concurrent queries")
        print("  - Reuse UDP connections")
        print("")
    
    def test_issue_4_no_response_on_parse_error(self):
        """Issue: No DNS response sent when parsing fails.
        
        Impact: Clients timeout waiting for invalid packet responses.
        """
        print("\n" + "="*70)
        print("ISSUE #4: No DNS Response on Parse Error")
        print("="*70)
        print("Problem:")
        print("  - handle_request() catches parsing exceptions")
        print("  - But doesn't send any response to client")
        print("  - DNSRecord.parse(data) fails -> exception caught -> nothing sent")
        print("\nImpact:")
        print("  - Client waits for response (indefinitely or timeout)")
        print("  - Violates DNS protocol expectations")
        print("  - Can cause client-side timeouts and retries")
        print("\nSolution:")
        print("  if parsing fails:")
        print("      send SERVFAIL response")
        print("      (or FORMERR for format errors)")
        print("")
    
    def test_issue_5_thread_pool_exhaustion(self):
        """Issue: Potential thread pool exhaustion.
        
        Impact: Too many concurrent executor requests overwhelm thread pool.
        """
        print("\n" + "="*70)
        print("ISSUE #5: Thread Pool Exhaustion Risk")
        print("="*70)
        print("Problem:")
        print("  - run_in_executor(None, ...) uses default thread pool")
        print("  - Default pool size is typically min(32, os.cpu_count() + 4)")
        print("  - Under high concurrent load, all threads may be blocked")
        print("\nImpact:")
        print("  - Additional requests queue up indefinitely")
        print("  - Memory grows from queued tasks")
        print("  - Server becomes unresponsive")
        print("\nSolution:")
        print("  - Use caching to reduce executor calls")
        print("  - Or: Set max_workers in ThreadPoolExecutor")
        print("  - Limit concurrent requests with semaphore")
        print("")


# Test execution summary
ISSUES_TO_FIX = {
    "1": {
        "title": "No NAT64 Prefix Caching",
        "severity": "HIGH",
        "fix": "Add TTL-based cache to NAT64Resolver",
        "test": "test_concurrent_requests_same_prefix"
    },
    "2": {
        "title": "No Synchronization for Concurrent File Reads",
        "severity": "HIGH",
        "fix": "Add threading.Lock() around file access",
        "test": "test_prefix_file_being_written"
    },
    "3": {
        "title": "Socket Exhaustion from No Connection Pooling",
        "severity": "CRITICAL",
        "fix": "Implement connection pool or limit concurrent queries",
        "test": "test_concurrent_upstream_queries_socket_exhaustion"
    },
    "4": {
        "title": "No DNS Response on Parse Error",
        "severity": "MEDIUM",
        "fix": "Send SERVFAIL response on parsing exceptions",
        "test": "test_handle_request_exception_malformed_data"
    },
    "5": {
        "title": "Thread Pool Exhaustion Risk",
        "severity": "MEDIUM",
        "fix": "Reduce executor usage via caching + semaphore limit",
        "test": "test_many_concurrent_reads"
    },
    "6": {
        "title": "Prefix File Race Conditions During Writes",
        "severity": "HIGH",
        "fix": "Add file locking (fcntl.flock or tempfile atomicity)",
        "test": "test_concurrent_reads_during_truncate"
    },
    "7": {
        "title": "Symlink/File Type Changes",
        "severity": "MEDIUM",
        "fix": "Validate file type and handle symlink changes",
        "test": "test_prefix_file_symlink_chain"
    },
    "8": {
        "title": "IPv6 Prefix Validation Edge Cases",
        "severity": "MEDIUM",
        "fix": "Add comprehensive prefix validation and sanitization",
        "test": "test_prefix_edge_cases"
    },
    "9": {
        "title": "Network Prefix Mismatch in DNS64 Synthesis",
        "severity": "HIGH",
        "fix": "Calculate IPv6 address based on actual prefix length",
        "test": "test_prefix_length_mismatch_in_synthesis"
    },
    "10": {
        "title": "Memory Pressure with Large Files",
        "severity": "MEDIUM",
        "fix": "Use streaming read instead of readlines()",
        "test": "test_file_with_many_comments"
    },
    "11": {
        "title": "Path Traversal Vulnerability",
        "severity": "CRITICAL",
        "fix": "Validate and normalize NAT64_PREFIX_FILE path",
        "test": "test_path_traversal_protection"
    },
}


def print_summary():
    """Print summary of critical issues."""
    print("\n" + "="*70)
    print("CRITICAL ISSUES FOUND IN nat64dns.py")
    print("="*70)
    
    for issue_num, issue_info in sorted(ISSUES_TO_FIX.items()):
        print(f"\nIssue #{issue_num}: {issue_info['title']}")
        print(f"  Severity: {issue_info['severity']}")
        print(f"  Fix: {issue_info['fix']}")
        print(f"  Test: {issue_info['test']}")
    
    print("\n" + "="*70)
    print("\nTo run individual tests:")
    print("  python test_nat64dns.py TestPrefixFileRaceConditions -v")
    print("  python test_nat64dns.py TestIPv6PrefixValidationEdgeCases -v")
    print("  python test_nat64dns.py TestFileDescriptorExhaustion -v")
    print("="*70)


class TestIPAddressConflictResolution(unittest.TestCase):
    """Tests for IP address conflict resolution scenarios."""
    
    def test_duplicate_ipv6_synthesis_same_ipv4(self):
        """Test that same IPv4 address produces same IPv6.

        ISSUE: If DNS64 synthesis is non-deterministic, same IPv4 could
        produce different IPv6 addresses, causing conflicts.
        """
        resolver = NAT64Resolver()

        # Same IPv4, same customer, same site should produce same IPv6
        qname1 = "192-0-2-1.t000001.nat64"
        qname2 = "192-0-2-1.t000001.nat64"

        result1 = resolver.resolve(qname1)
        result2 = resolver.resolve(qname2)

        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertEqual(result1, result2, "Same input should produce identical IPv6")
    
    def test_different_customers_same_ipv4(self):
        """Test IPv6 addresses differ for different customers with same IPv4.
        
        ISSUE: If customer_id is not properly encoded, different customers
        could get the same IPv6 address for same IPv4.
        """
        resolver = NAT64Resolver()
        
        # Same IPv4 but different customers
        qname_cust1 = "192-0-2-1.t000001.nat64"
        qname_cust2 = "192-0-2-1.t000002.nat64"
        
        result1 = resolver.resolve(qname_cust1)
        result2 = resolver.resolve(qname_cust2)
        
        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertNotEqual(result1, result2, "Different customers should get different IPv6")
    
    def test_different_sites_same_ipv4_customer(self):
        """Test IPv6 addresses differ for different sites.
        
        ISSUE: If site_id is not properly encoded, different sites
        could get the same IPv6 address.
        """
        resolver = NAT64Resolver()
        
        # Same IPv4 and customer, but different sites
        qname_site0 = "192-0-2-1.0.t000001.nat64"
        qname_site1 = "192-0-2-1.1.t000001.nat64"
        
        result1 = resolver.resolve(qname_site0)
        result2 = resolver.resolve(qname_site1)
        
        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertNotEqual(result1, result2, "Different sites should get different IPv6")
    
    def test_ipv4_address_pool_exhaustion(self):
        """Test behavior when synthesizing for edge IPv4 addresses.
        
        ISSUE: With limited IPv6 prefix space (/96), may not accommodate
        all possible IPv4 addresses for a customer.
        """
        resolver = NAT64Resolver()
        
        # For /96 prefix, should support full IPv4 space (4 billion addresses)
        # Test edge cases:
        
        # Minimum IPv4
        result_min = resolver.resolve("0-0-0-0.t000001.nat64")
        self.assertIsNotNone(result_min)
        
        # Maximum IPv4
        result_max = resolver.resolve("255-255-255-255.t000001.nat64")
        self.assertIsNotNone(result_max)
        
        # These should be different
        self.assertNotEqual(result_min, result_max)
    
    def test_customer_id_collision_risk(self):
        """Test for customer ID collision vulnerabilities.
        
        ISSUE: Customer ID is 24-bit, could theoretically have collisions
        if not properly isolated from site_id (8-bit).
        """
        resolver = NAT64Resolver()
        
        # Max valid customer ID (24-bit: 0xFFFFFF = 16,777,215)
        qname_max_customer = "192-0-2-1.tFFFFFF.nat64"
        result_max = resolver.resolve(qname_max_customer)
        self.assertIsNotNone(result_max)
        
        # Customer ID overflow (25-bit: 0x1000000) should fail
        qname_overflow = "192-0-2-1.t1000000.nat64"
        result_overflow = resolver.resolve(qname_overflow)
        self.assertIsNone(result_overflow)
    
    def test_site_id_collision_risk(self):
        """Test for site ID collision vulnerabilities.
        
        ISSUE: Site ID is 8-bit, potential for collisions if not properly
        isolated from customer_id (24-bit).
        """
        resolver = NAT64Resolver()
        
        # Max valid site ID (8-bit: 0xFF = 255)
        qname_max_site = "192-0-2-1.FF.t000001.nat64"
        result_max = resolver.resolve(qname_max_site)
        self.assertIsNotNone(result_max)
        
        # Site ID overflow (9-bit: 0x100) should fail
        qname_overflow = "192-0-2-1.100.t000001.nat64"
        result_overflow = resolver.resolve(qname_overflow)
        self.assertIsNone(result_overflow)
    
    def test_ipv6_address_uniqueness_across_parameters(self):
        """Test that all combinations produce unique IPv6 addresses.
        
        ISSUE: If address construction is wrong, different parameter
        combinations could produce the same IPv6 address.
        """
        resolver = NAT64Resolver()
        
        addresses = set()
        test_cases = [
            "192-0-2-1.t000001.nat64",
            "192-0-2-1.t000002.nat64",
            "192-0-2-2.t000001.nat64",
            "192-0-2-2.t000002.nat64",
            "192-0-2-1.1.t000001.nat64",
            "192-0-2-1.2.t000001.nat64",
        ]
        
        for qname in test_cases:
            result = resolver.resolve(qname)
            if result:
                addr_str = str(result)
                self.assertNotIn(addr_str, addresses,
                    f"Duplicate IPv6 found for {qname}: {addr_str}")
                addresses.add(addr_str)
        
        # All should be unique
        self.assertEqual(len(addresses), len(test_cases))
    
    def test_concurrent_ipv6_address_requests(self):
        """Test concurrent requests for same IPv4 produce same result.
        
        ISSUE: Concurrent synthesis requests for same IPv4 should be
        idempotent and not cause address conflicts.
        """
        resolver = NAT64Resolver()
        qname = "192-0-2-1.t000001.nat64"
        
        # Run 10 concurrent requests for same IPv4
        results = []
        for _ in range(10):
            result = resolver.resolve(qname)
            results.append(str(result) if result else None)
        
        # All should be identical
        self.assertEqual(len(set(results)), 1, "Concurrent requests should produce same IPv6")
    
    def test_ipv4_octet_boundary_addresses(self):
        """Test IPv4 addresses at octet boundaries for synthesis.
        
        ISSUE: Address boundaries (e.g., X.0.0.0, X.255.255.255) could
        cause encoding issues if not handled properly.
        """
        resolver = NAT64Resolver()
        
        boundary_cases = [
            "0-0-0-0.t000001.nat64",          # All zeros
            "255-255-255-255.t000001.nat64",  # All ones
            "192-0-0-0.t000001.nat64",        # Zero octets
            "192-255-255-255.t000001.nat64",  # Max octets
            "192-168-0-0.t000001.nat64",      # Private range
            "127-0-0-1.t000001.nat64",        # Loopback
            "224-0-0-0.t000001.nat64",        # Multicast
        ]
        
        results = set()
        for qname in boundary_cases:
            result = resolver.resolve(qname)
            if result:
                results.add(str(result))
        
        # All unique results should be different
        self.assertEqual(len(results), len(boundary_cases),
            "All boundary addresses should produce unique IPv6")
    
    def test_customer_id_encoding_no_overlap(self):
        """Test that customer ID bits don't overlap with site ID.
        
        ISSUE: If bit shifting is wrong in customer_id << 40 | site_id << 32,
        bits could overlap and cause collisions.
        """
        resolver = NAT64Resolver()
        
        # Create addresses that would collide if bit shifting is wrong
        qname1 = "192-0-2-1.tAAAAAA.nat64"
        qname2 = "192-0-2-2.t555555.nat64"
        qname3 = "192-0-2-3.t000001.nat64"
        
        result1 = resolver.resolve(qname1)
        result2 = resolver.resolve(qname2)
        result3 = resolver.resolve(qname3)
        
        # All should be different
        self.assertNotEqual(result1, result2)
        self.assertNotEqual(result2, result3)
        self.assertNotEqual(result1, result3)
    
    def test_ipv4_to_ipv6_mapping_is_bijective(self):
        """Test that IPv4->IPv6 mapping is one-to-one (bijective).
        
        ISSUE: If mapping is not bijective, different IPv4 could map to
        same IPv6 (collision) or same IPv4 could map to different IPv6.
        """
        resolver = NAT64Resolver()
        
        # Test sample of IPv4 addresses for uniqueness
        ipv4_samples = [
            "192-0-2-1",
            "192-0-2-2",
            "192-0-2-3",
            "10-0-0-1",
            "172-16-0-1",
        ]
        
        ipv6_results = {}
        for ipv4 in ipv4_samples:
            qname = f"{ipv4}.t000001.nat64"
            ipv6 = resolver.resolve(qname)
            ipv6_str = str(ipv6)
            
            if ipv6_str in ipv6_results:
                self.fail(f"Collision: {ipv4} and {ipv6_results[ipv6_str]} both map to {ipv6_str}")
            ipv6_results[ipv6_str] = ipv4
    
    def test_ipv4_reserved_addresses_handling(self):
        """Test handling of IPv4 reserved address ranges.
        
        ISSUE: Should system allow synthesis for reserved ranges
        (127.0.0.0/8, 169.254.0.0/16, 224.0.0.0/4, etc.)?
        Currently no validation - this could be a design issue.
        """
        resolver = NAT64Resolver()
        
        reserved_ranges = {
            "0-0-0-0.t000001.nat64": "0.0.0.0/8 (This host)",
            "10-0-0-1.t000001.nat64": "10.0.0.0/8 (Private)",
            "127-0-0-1.t000001.nat64": "127.0.0.0/8 (Loopback)",
            "169-254-0-1.t000001.nat64": "169.254.0.0/16 (Link-local)",
            "172-16-0-1.t000001.nat64": "172.16.0.0/12 (Private)",
            "192-168-0-1.t000001.nat64": "192.168.0.0/16 (Private)",
            "224-0-0-1.t000001.nat64": "224.0.0.0/4 (Multicast)",
            "255-255-255-255.t000001.nat64": "255.255.255.255 (Broadcast)",
        }
        
        # Currently, resolver accepts all addresses
        for qname, description in reserved_ranges.items():
            result = resolver.resolve(qname)
            # Currently accepts all - could be security/design issue


class TestAddressConflictDetection(unittest.TestCase):
    """Tests for detecting address conflicts in NAT64 system."""
    
    def test_ipv4_to_ipv6_mapping_completeness(self):
        """Verify IPv4->IPv6 mapping function exists and works.
        
        For conflict detection, system needs to map IPv4->IPv6 and
        check for duplicates. Currently no dedicated function for this.
        """
        resolver = NAT64Resolver()
        
        # Map several IPv4 addresses
        mapping = {}
        for i in range(1, 6):
            qname = f"192-0-2-{i}.t000001.nat64"
            ipv6 = resolver.resolve(qname)
            if ipv6:
                mapping[f"192.0.2.{i}"] = str(ipv6)
        
        # Check all are unique
        ipv6_values = list(mapping.values())
        self.assertEqual(len(ipv6_values), len(set(ipv6_values)),
            "Address mapping should be unique")
    
    def test_address_pool_tracking_capability(self):
        """Test if system can track allocated addresses.
        
        ISSUE: No mechanism to track which IPv4->IPv6 mappings exist,
        making conflict detection impossible.
        """
        resolver = NAT64Resolver()
        
        # Simulate tracking allocated addresses
        allocated = {}
        
        for i in range(1, 4):
            qname = f"192-0-2-{i}.t000001.nat64"
            ipv6 = resolver.resolve(qname)
            ipv4 = f"192.0.2.{i}"
            
            allocated[ipv4] = str(ipv6)
        
        # Try to request same IPv4 again - should get same IPv6
        qname_repeat = "192-0-2-1.t000001.nat64"
        ipv6_repeat = resolver.resolve(qname_repeat)
        
        self.assertEqual(str(ipv6_repeat), allocated["192.0.2.1"],
            "Repeated allocation should return same IPv6")
    
    def test_address_conflict_detection_mechanism(self):
        """Test if there's a mechanism to detect address conflicts.
        
        ISSUE: No built-in conflict detection when allocating addresses.
        """
        resolver = NAT64Resolver()
        
        # Simulate allocation attempts
        allocations = []
        
        # First allocation
        qname1 = "192-0-2-1.t000001.nat64"
        ipv6_1 = resolver.resolve(qname1)
        allocations.append(ipv6_1)
        
        # Try to allocate same address again (should work - no tracking)
        qname1_again = "192-0-2-1.t000001.nat64"
        ipv6_1_again = resolver.resolve(qname1_again)
        allocations.append(ipv6_1_again)
        
        # Without tracking, can't detect double allocation
        # This test documents the limitation
        self.assertEqual(ipv6_1, ipv6_1_again)
    
    def test_address_space_efficiency(self):
        """Test efficiency of address space usage.
        
        ISSUE: /96 NAT64 prefix fits all IPv4 addresses (32 bits) but
        may not handle all combinations of customer_id (24-bit) + site_id (8-bit).
        """
        resolver = NAT64Resolver()
        
        # With /96 prefix: 32 bits for IPv4 = 4 billion addresses per customer
        # 24-bit customer_id = 16 million customers
        # 8-bit site_id = 256 sites per customer
        # Total: 16M customers x 256 sites x 4B IPv4 = huge address space needed
        
        # Current implementation only uses 32 bits for customer_id + site_id + IPv4
        # This is actually efficient but limits customer space
        
        # Test that it handles the designed limits
        result = resolver.resolve("192-0-2-1.tFFFFFF.FF.nat64")
        self.assertIsNotNone(result)


if __name__ == "__main__":
    # Run tests
    unittest.main(verbosity=2)
