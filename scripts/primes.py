#!/usr/bin/env python3
"""Print all prime numbers up to 1,000,000 using an optimized Sieve of Eratosthenes.

Two implementations are provided:
  1. numpy-based  — fastest; vectorised boolean ops in C
  2. bytearray    — pure-Python fallback, no dependencies

The script auto-selects numpy if available, runs a small validation first,
then prints all primes with per-phase timing.
"""

import sys
import time

# ---------------------------------------------------------------------------
# Implementation 1: numpy (preferred — all heavy lifting happens in C)
# ---------------------------------------------------------------------------
try:
    import numpy as np

    def sieve_numpy(limit):
        """Sieve of Eratosthenes using a numpy boolean array.

        numpy.nonzero extracts prime indices in a single vectorised pass,
        avoiding any Python-level iteration over the array.
        """
        # One bool per integer; numpy packs this efficiently
        is_prime = np.ones(limit + 1, dtype=np.bool_)
        is_prime[:2] = False

        # Mark composites: only check up to sqrt(limit)
        for i in range(2, int(limit**0.5) + 1):
            if is_prime[i]:
                # Vectorised slice — sets millions of entries in one C call
                is_prime[i*i::i] = False

        # np.nonzero returns indices where True — no Python loop needed
        return np.nonzero(is_prime)[0]

    _sieve_fn = sieve_numpy
    _engine = "numpy"

except ImportError:
    _sieve_fn = None
    _engine = None

# ---------------------------------------------------------------------------
# Implementation 2: pure-Python bytearray fallback
# ---------------------------------------------------------------------------
def sieve_bytearray(limit):
    """Sieve of Eratosthenes using a bytearray (no dependencies).

    bytearray gives ~1 byte per entry with cache-friendly sequential access.
    Slice assignment delegates composite-zeroing to C's memset.
    """
    sieve = bytearray(b'\x01') * (limit + 1)
    sieve[0] = sieve[1] = 0

    for i in range(2, int(limit**0.5) + 1):
        if sieve[i]:
            # Bulk-zero all multiples of i from i² onward
            sieve[i*i::i] = bytearray(len(sieve[i*i::i]))

    # Collect prime indices via list comprehension
    return [n for n in range(2, limit + 1) if sieve[n]]

if _sieve_fn is None:
    _sieve_fn = sieve_bytearray
    _engine = "bytearray"


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------
KNOWN_SMALL_PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]

def validate(sieve_fn):
    """Quick sanity check against known primes up to 50."""
    result = list(sieve_fn(50))
    assert result == KNOWN_SMALL_PRIMES, f"Validation failed: {result}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    limit = 1_000_000

    # Step 0: Validate correctness on a small input
    validate(_sieve_fn)

    # Step 1: Run the sieve
    t0 = time.perf_counter()
    primes = _sieve_fn(limit)
    t_sieve = time.perf_counter() - t0

    # Step 2: Build output string — map+join is faster than repeated str()
    t1 = time.perf_counter()
    out = '\n'.join(map(str, primes))
    t_build = time.perf_counter() - t1

    # Step 3: Single write to stdout via the raw buffer (avoids per-line encoding)
    t2 = time.perf_counter()
    sys.stdout.buffer.write(out.encode())
    sys.stdout.buffer.write(b'\n')
    t_write = time.perf_counter() - t2

    t_total = time.perf_counter() - t0

    # Timing summary on stderr so it doesn't pollute the prime stream
    print(f"\n--- Timing ({_engine} engine, primes up to {limit:,}) ---", file=sys.stderr)
    print(f"  Sieve:       {t_sieve:.4f}s", file=sys.stderr)
    print(f"  Str build:   {t_build:.4f}s", file=sys.stderr)
    print(f"  I/O write:   {t_write:.4f}s", file=sys.stderr)
    print(f"  Total:       {t_total:.4f}s", file=sys.stderr)
    print(f"  Found:       {len(primes):,} primes", file=sys.stderr)


if __name__ == '__main__':
    main()
