# nat64dns.py Comprehensive Test Suite

## Overview
A complete test suite for `nat64dns.py` that identifies 11 critical concurrency, race condition, and security issues.

## Test File Location
`files/system/usr/scripts/test_nat64dns.py`

## Test Classes & Coverage

### 1. **TestNAT64Resolver** - Pure Logic Tests
Tests the NAT64 custom resolution logic without async/concurrency issues.

**Tests:**
- `test_resolve_valid_nat64_name()` - Valid custom NAT64 resolution
- `test_resolve_without_site_id()` - Resolution without site ID
- `test_resolve_invalid_qname()` - Non-NAT64 domain handling
- `test_resolve_invalid_ipv4()` - Invalid IPv4 detection
- `test_resolve_invalid_customer_id_format()` - Hex format validation
- `test_resolve_customer_id_overflow()` - Overflow boundary checking

**Status:** ✓ Will PASS

---

### 2. **TestLoadNAT64PrefixAsync** - Basic Async Tests
Tests the prefix loading function with basic scenarios.

**Tests:**
- `test_load_nat64_prefix_valid()` - Valid prefix file read
- `test_load_nat64_prefix_missing_file()` - FileNotFoundError handling
- `test_load_nat64_prefix_invalid_format()` - Invalid IPv6 format handling
- `test_file_read_permission_error()` - PermissionError handling
- `test_concurrent_requests_same_prefix()` - **FAILS**: No caching (ISSUE #1, #2)
- `test_concurrent_requests_different_prefix_files()` - **FAILS**: Inconsistent prefixes (ISSUE #1)

**Status:** ✓ 4 PASS, ✗ 2 FAIL

---

### 3. **TestQueryUpstreamAsync** - Socket Management
Tests upstream DNS query socket handling.

**Tests:**
- `test_query_upstream_success()` - Successful upstream query
- `test_concurrent_upstream_queries_socket_exhaustion()` - **FAILS**: Socket exhaustion (ISSUE #3)

**Status:** ✗ Demonstrates socket pool exhaustion issue

---

### 4. **TestResolveUpstreamDNS64Concurrent** - DNS64 Synthesis
Tests DNS64 response synthesis under concurrency.

**Tests:**
- `test_resolve_upstream_dns64_empty_response()` - Empty response handling
- `test_resolve_upstream_dns64_concurrent()` - **FAILS**: Socket creation per request (ISSUE #3)

**Status:** ✗ Demonstrates socket exhaustion in DNS64 path

---

### 5. **TestHandleRequestException** - Protocol Error Handling
Tests DNS protocol compliance during errors.

**Tests:**
- `test_handle_request_exception_malformed_data()` - **FAILS**: No SERVFAIL response (ISSUE #4)
- `test_handle_request_aaaa_custom_resolution()` - ✓ Works
- `test_handle_request_missing_nat64_prefix()` - ✓ Works
- `test_mixed_resolution_workflow()` - ✓ Sequential flow works

**Status:** ✗ 1 FAIL (protocol violation)

---

### 6. **TestPrefixFileRaceConditions** - File Write Race Conditions
Tests race conditions when the prefix file is being modified.

**Tests:**
- `test_prefix_file_being_written()` - **FAILS**: File being written during read (ISSUE #6)
  - Concurrent reads during file writes
  - Potential partial reads or truncated data
  
- `test_concurrent_reads_during_truncate()` - **FAILS**: File truncation race (ISSUE #6)
  - Reads while file is being truncated
  - Some requests get None (empty file)

**Status:** ✗ Demonstrates file locking needed

---

### 7. **TestSymlinkFileTypeChanges** - Symlink Handling
Tests symlink changes and file type transitions.

**Tests:**
- `test_prefix_file_symlink_chain()` - **FAILS**: Symlink target changes (ISSUE #7)
  - Symlink chain changes between reads
  - May read invalid data if target changes

**Status:** ✗ Demonstrates symlink validation needed

---

### 8. **TestIPv6PrefixValidationEdgeCases** - Prefix Format Validation
Tests IPv6 prefix parsing with edge cases.

**Tests:**
- `test_prefix_edge_cases()` - **FAILS**: Multiple edge cases (ISSUE #8)
  - Default route `::/0` - ✓ Works
  - Standard prefix `/32` - ✓ Works
  - Single address `/128` - ✓ Works
  - Inline comments - ✓ Works
  - Extra spaces - ✓ Works
  - Invalid mask (>128) - ✓ Caught
  - Multiple prefixes - Takes first ✓
  - No mask - ✓ Caught
  - No address - ✓ Caught
  - IPv4 prefix - ✓ Caught
  - But: "prefix 2001:db8::" interpreted as /128 instead of error

**Status:** ✗ Minor validation issues

---

### 9. **TestFileDescriptorExhaustion** - Resource Exhaustion
Tests file descriptor limits under high concurrency.

**Tests:**
- `test_many_concurrent_reads()` - **WARNS**: 50 concurrent file opens (ISSUE #5)
  - Each request opens file descriptor
  - No connection pooling/reuse
  - Reports success count vs failures

**Status:** ⚠️ Works but inefficient

---

### 10. **TestNetworkPrefixMismatch** - DNS64 Synthesis Correctness
Tests DNS64 address synthesis with different prefix lengths.

**Tests:**
- `test_prefix_length_mismatch_in_synthesis()` - **FAILS**: Non-/96 prefixes (ISSUE #9)
  - `/96` prefix works correctly
  - `/64` prefix may cause wrong IPv6 synthesis
  - `/32` prefix causes incorrect address calculation

**Status:** ✗ Assumption of /96 prefix hardcoded

---

### 11. **TestThreadPoolStarvation** - Thread Pool Management
Tests behavior under thread pool saturation.

**Tests:**
- `test_executor_callback_handling()` - **WARNS**: 20 concurrent reads (ISSUE #5)
  - Shows timing impact of no caching
  - Thread pool queuing effects visible
  - Caching would significantly improve performance

**Status:** ⚠️ Performance impact demonstrated

---

### 12. **TestMemoryPressure** - Large File Handling
Tests memory efficiency with large configuration files.

**Tests:**
- `test_file_with_many_comments()` - **WARNS**: 10K+ lines before prefix (ISSUE #10)
  - `readlines()` loads entire file to memory
  - Works but inefficient for large files
  - Streaming read would be better

**Status:** ⚠️ Works but memory inefficient

---

### 13. **TestSecurityPathTraversal** - Path Validation
Tests vulnerability to path traversal attacks.

**Tests:**
- `test_path_traversal_protection()` - **FAILS**: No path validation (ISSUE #11)
  - Could read arbitrary files if NAT64_PREFIX_FILE path controlled
  - No absolute path enforcement
  - No symlink resolution limits

**Status:** ✗ Security vulnerability

---

### 14. **TestConcurrencyIssuesSummary** - Issue Documentation
Documents all 11 issues with severity and remediation.

**Status:** Reference documentation

---

## Critical Issues Found

| # | Issue | Severity | Test |
|---|-------|----------|------|
| 1 | No NAT64 Prefix Caching | HIGH | `test_concurrent_requests_same_prefix` |
| 2 | No File Synchronization | HIGH | `test_prefix_file_being_written` |
| 3 | Socket Exhaustion | CRITICAL | `test_concurrent_upstream_queries_socket_exhaustion` |
| 4 | No Error Response | MEDIUM | `test_handle_request_exception_malformed_data` |
| 5 | Thread Pool Exhaustion | MEDIUM | `test_many_concurrent_reads` |
| 6 | File Write Race Conditions | HIGH | `test_concurrent_reads_during_truncate` |
| 7 | Symlink Handling | MEDIUM | `test_prefix_file_symlink_chain` |
| 8 | Prefix Validation | MEDIUM | `test_prefix_edge_cases` |
| 9 | Prefix Length Mismatch | HIGH | `test_prefix_length_mismatch_in_synthesis` |
| 10 | Memory Pressure | MEDIUM | `test_file_with_many_comments` |
| 11 | Path Traversal | CRITICAL | `test_path_traversal_protection` |

---

## Running the Tests

### Run All Tests
```bash
python test_nat64dns.py -v
```

### Run Specific Test Class
```bash
python test_nat64dns.py TestPrefixFileRaceConditions -v
python test_nat64dns.py TestIPv6PrefixValidationEdgeCases -v
python test_nat64dns.py TestFileDescriptorExhaustion -v
python test_nat64dns.py TestNetworkPrefixMismatch -v
```

### Run Single Test
```bash
python test_nat64dns.py TestNAT64Resolver.test_resolve_valid_nat64_name -v
python test_nat64dns.py TestPrefixFileRaceConditions.test_prefix_file_being_written -v
```

---

## Expected Results Summary

### Will PASS (8 tests)
- All TestNAT64Resolver tests (pure logic)
- TestLoadNAT64PrefixAsync basic tests
- TestHandleRequestException basic tests
- TestMixedResolutionWorkflow

### Will FAIL (6 tests)
- TestLoadNAT64PrefixAsync.test_concurrent_requests_same_prefix
- TestLoadNAT64PrefixAsync.test_concurrent_requests_different_prefix_files
- TestQueryUpstreamAsync.test_concurrent_upstream_queries_socket_exhaustion
- TestHandleRequestException.test_handle_request_exception_malformed_data
- TestNetworkPrefixMismatch.test_prefix_length_mismatch_in_synthesis
- TestSecurityPathTraversal.test_path_traversal_protection

### Will WARN (3 tests)
- TestPrefixFileRaceConditions (shows race conditions)
- TestSymlinkFileTypeChanges (shows symlink issues)
- TestIPv6PrefixValidationEdgeCases (edge case handling)
- TestFileDescriptorExhaustion (shows inefficiency)
- TestThreadPoolStarvation (shows performance impact)
- TestMemoryPressure (shows memory inefficiency)

---

## Recommended Fixes

### Priority: CRITICAL
1. **Connection Pooling** - Limit concurrent UDP sockets
2. **Path Validation** - Secure NAT64_PREFIX_FILE path handling

### Priority: HIGH
3. **Prefix Caching** - Cache with TTL to avoid repeated file reads
4. **File Locking** - Use fcntl.flock() for concurrent read safety
5. **Prefix Length** - Calculate DNS64 synthesis based on actual prefix length

### Priority: MEDIUM
6. **Error Responses** - Send SERVFAIL on parsing exceptions
7. **Thread Limits** - Use semaphore to limit concurrent executor tasks
8. **Symlink Handling** - Validate file type and resolve symlinks safely
9. **Validation** - Comprehensive prefix format validation
10. **Memory** - Use streaming read instead of readlines()
11. **Protocol** - Complete DNS protocol error handling
