"""Cache/compact economics for Kesha (Opus 4.6, 200K ctx, Max subscription virtual $).
All prices per Anthropic prompt-caching docs + cache-optimization/research.md (measured)."""

# --- constants (grounded) ---
CTX_WINDOW = 200_000            # Opus 4.6, no [1m]
BASE_IN = 5.0 / 1e6            # $/token input base (Opus)
OUT = 25.0 / 1e6              # $/token output
READ = 0.1 * BASE_IN         # cache read 0.1x
WRITE_1H = 2.0 * BASE_IN     # cache create 1h 2.0x
AVG_OUT_TURN = 338           # median 4.6 output tok/turn (measured, research.md)
SUMMARY_TOK = 9120 / 4       # ~2280 tok, measured Kesha compact summary (9120 chars / 4)

def tokens(pct):  # context tokens at pct%
    return pct/100 * CTX_WINDOW

def cached_turn(pct):
    """Warm cache: full context read at 0.1x + output at full price."""
    t = tokens(pct)
    return t * READ + AVG_OUT_TURN * OUT

def cold_turn(pct):
    """Evicted: full context re-billed at cache-create 2.0x (1h) + output."""
    t = tokens(pct)
    return t * WRITE_1H + AVG_OUT_TURN * OUT

def compact_cost(pct):
    """Compact = read warm context (0.1x) to summarize + generate summary (output)
    + write the small preamble as new cache-create (2.0x). Context after ≈ summary size."""
    t = tokens(pct)
    read_ctx = t * READ                    # model reads full context to summarize (warm)
    gen_summary = SUMMARY_TOK * OUT        # summary output tokens (full price)
    write_preamble = SUMMARY_TOK * WRITE_1H  # new small prefix cache-create
    return read_ctx + gen_summary + write_preamble

def after_compact_turn():
    """Next turn after compact: context ≈ summary tokens, warm (just written)."""
    return SUMMARY_TOK * READ + AVG_OUT_TURN * OUT

print(f"{'ctx%':>5} {'tokens':>8} {'cached_turn':>12} {'cold_turn':>11} {'cold/cached':>11} {'compact':>10} {'compact+next':>13}")
for pct in range(10, 101, 10):
    ct = cached_turn(pct)
    cold = cold_turn(pct)
    comp = compact_cost(pct)
    comp_next = comp + after_compact_turn()
    print(f"{pct:>5} {tokens(pct):>8.0f} ${ct*1000:>10.4f}m ${cold*1000:>9.4f}m {cold/ct:>10.1f}x ${comp*1000:>8.4f}m ${comp_next*1000:>11.4f}m")

print("\n=== BREAKEVEN: preventive compact vs letting cache go cold ===")
print("Scenario: idle >55min. Option A: compact now → next msg cheap (warm small ctx).")
print("          Option B: do nothing → if user returns >60min, cold_turn (re-bill).")
print(f"{'ctx%':>5} {'A=compact+next':>15} {'B=cold_turn':>12} {'A cheaper?':>11} {'B=cached(warm)':>15}")
for pct in range(10, 101, 10):
    A = compact_cost(pct) + after_compact_turn()   # compact + next msg on small warm ctx
    B_cold = cold_turn(pct)                          # user returns after eviction
    B_warm = cached_turn(pct)                         # user returns while still warm
    cheaper = "YES" if A < B_cold else "no"
    print(f"{pct:>5} ${A*1000:>13.4f}m ${B_cold*1000:>10.4f}m {cheaper:>11} ${B_warm*1000:>13.4f}m")

print("\n=== The REAL question: expected value given P(return within TTL) ===")
print("Measured: 92% of user gaps <60min (return warm), 8% >60min (would go cold).")
print("Preventive compact PAYS only for the 8% cold case; for 92% it WASTES a compact.\n")
P_cold = 0.08
for pct in [30, 40, 50]:
    # EV of NO compact: 92% warm cached turn + 8% cold turn
    ev_nocompact = 0.92*cached_turn(pct) + 0.08*cold_turn(pct)
    # EV of preventive compact at 55min: always pay compact, next turn small+warm
    ev_compact = compact_cost(pct) + after_compact_turn()
    print(f"ctx={pct}%: EV(no-compact)=${ev_nocompact*1000:.4f}m  EV(preventive-compact)=${ev_compact*1000:.4f}m  "
          f"→ {'compact wins' if ev_compact<ev_nocompact else 'NO-COMPACT wins'}")

print("\n=== P(cold) breakeven: at what cold-probability does preventive compact win? ===")
def cached_turn(pct): return tokens(pct)*READ + AVG_OUT_TURN*OUT
def cold_turn(pct): return tokens(pct)*WRITE_1H + AVG_OUT_TURN*OUT
for pct in [30,40,50,70,100]:
    comp = compact_cost(pct) + after_compact_turn()
    warm = cached_turn(pct); cold = cold_turn(pct)
    # EV_nocompact(p) = (1-p)*warm + p*cold ; EV_compact = comp (const)
    # solve comp = (1-p)warm + p*cold → p* = (comp - warm)/(cold - warm)
    p_star = (comp - warm)/(cold - warm)
    print(f"ctx={pct}%: compact wins only if P(user returns COLD) > {p_star*100:.0f}%  (measured P_cold=8%)")

print("\n=== SMARTER: cheap keep-alive turn (2-word ping) instead of compact ===")
# keep-alive = tiny turn: reads full warm ctx at 0.1x + ~5 output tokens. Refreshes TTL.
def keepalive(pct): return tokens(pct)*READ + 5*OUT
for pct in [30,40,50,100]:
    ka = keepalive(pct); comp = compact_cost(pct)
    print(f"ctx={pct}%: keep-alive ping=${ka*1000:.4f}m vs compact=${comp*1000:.4f}m  → keep-alive {comp/ka:.0f}x cheaper, and KEEPS context")
