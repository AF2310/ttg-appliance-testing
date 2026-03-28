# ttg-appliance-testing
Testing DNS server 
1. Client sends  query.
2. Server checks if query matches custom `.nat64` scheme.
3. If matched: returns IPv6 directly.
4. If not matched: it turns IPv4 DNS answers into IPv6 answers,combining the NAT64 prefix with the IPv4 address.
5. Sends DNS reply.
## adittions after testing 
- **Prefix loading**: reads `prefix ...` from `/etc/tayga/default.conf`.
- **Caching**: After reading that prefix once, it keeps it in memory so it doesn’t re-read the file on every DNS request.
- **Safety**:
  - before reading the prefix file, the code follows that shortcut to the real file path, so it knows exactly which file it is reading,
  - If many requests arrive at once, only a limited number are allowed to read the prefix file at the same time.,
  - limits how many DNS lookups it sends to the DNS server at once. This avoids flooding sockets/threads and keeps the service stable under traffic.
- **DNS64**:
  - It first checks that the NAT64 prefix has enough space to hold an IPv4 address (32 bits).,
  - Then it builds the IPv6 answer by placing the IPv4 value into the last 32 bits of the IPv6 address.
