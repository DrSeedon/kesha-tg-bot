"""CORRECTED model: compact as 'reset before sleep' to make the INEVITABLE cold-start cheap.
User's framing: at 55min idle, cold-start is ~92% certain (measured). Question is NOT
compact-vs-stay-warm, but: cold-start on X% ctx  vs  compact-now + cold-start on ~1% ctx."""

CTX_WINDOW = 200_000
BASE_IN = 5.0/1e6; OUT = 25.0/1e6
READ = 0.1*BASE_IN; WRITE_1H = 2.0*BASE_IN
AVG_OUT_TURN = 338
SUMMARY_TOK = 9120/4          # ~2280 tok measured compact summary
SUMMARY_PCT = SUMMARY_TOK/CTX_WINDOW*100   # ~1.14% of window

def tok(pct): return pct/100*CTX_WINDOW

def cold_start(pct):
    """First turn after cache eviction: full prefix re-billed at create 2.0x + output."""
    return tok(pct)*WRITE_1H + AVG_OUT_TURN*OUT

def compact_now(pct):
    """Compact at 55min while cache still WARM: read ctx (0.1x) + gen summary (out) + write preamble (2x)."""
    return tok(pct)*READ + SUMMARY_TOK*OUT + SUMMARY_TOK*WRITE_1H

def cold_start_after_compact():
    """After compact, ctx ≈ summary (~1.14%). Cold-start re-bills only that small prefix."""
    return SUMMARY_TOK*WRITE_1H + AVG_OUT_TURN*OUT

print("SUMMARY_PCT = %.2f%% of window (%d tok)\n" % (SUMMARY_PCT, SUMMARY_TOK))
print("Scenario: idle 55min → cold-start ~inevitable (P(gap>60|gap>55)=92.5% measured).")
print("Compare:  A) do nothing → cold_start(X%)   vs   B) compact now + cold_start(~1.1%)\n")
print(f"{'ctx%':>5} {'A: cold_start(X)':>17} {'B: compact+cold_small':>22} {'saving A-B':>11} {'B cheaper?':>11}")
breakeven=None
for pct in range(10,101,10):
    A = cold_start(pct)
    B = compact_now(pct) + cold_start_after_compact()
    save = A - B
    win = "YES" if B<A else "no"
    if breakeven is None and B<A: breakeven=pct
    print(f"{pct:>5} ${A*1000:>15.3f}m ${B*1000:>20.3f}m ${save*1000:>9.3f}m {win:>11}")

print(f"\nBreakeven: compact-before-sleep pays off at ctx >= ~{breakeven}% (rough grid).")

# fine breakeven: solve cold_start(x) = compact_now(x) + cold_start_after_compact()
# tok(x)*WRITE = tok(x)*READ + SUMMARY*OUT + SUMMARY*WRITE + SUMMARY*WRITE + OUT_TURN*OUT
# tok(x)*(WRITE-READ) = SUMMARY*OUT + 2*SUMMARY*WRITE + AVG_OUT_TURN*OUT
rhs = SUMMARY_TOK*OUT + 2*SUMMARY_TOK*WRITE_1H + AVG_OUT_TURN*OUT
tok_be = rhs/(WRITE_1H-READ)
pct_be = tok_be/CTX_WINDOW*100
print(f"Exact breakeven ctx = {pct_be:.1f}%  (above this, compact-before-sleep saves limit-burn)")

print("\n=== Expected value at 55min idle (P_return_cold = 92.5%) ===")
Pc=0.925
for pct in [30,40,50,70,80]:
    # A no-compact: 92.5% cold_start(X) + 7.5% warm cached resume (cheap)
    warm = tok(pct)*READ + AVG_OUT_TURN*OUT
    ev_A = Pc*cold_start(pct) + (1-Pc)*warm
    # B compact-now: pay compact always; 92.5% cold_small + 7.5% warm resume on small ctx
    warm_small = SUMMARY_TOK*READ + AVG_OUT_TURN*OUT
    ev_B = compact_now(pct) + (Pc*cold_start_after_compact() + (1-Pc)*warm_small)
    print(f"ctx={pct}%: EV(no-compact)=${ev_A*1000:.3f}m  EV(compact-before-sleep)=${ev_B*1000:.3f}m  "
          f"→ {'COMPACT wins' if ev_B<ev_A else 'no-compact wins'}")

print("\n=== Does keep-alive help for a LONG sleep? ===")
# keep-alive at 55min refreshes TTL by 60min. But sleep is median 113min, mean 220min.
# To bridge a 220min sleep with pings you'd need ~4 pings, each reading full ctx at 0.1x.
def keepalive(pct): return tok(pct)*READ + 5*OUT
for pct in [40,80]:
    sleep_min = 220  # measured mean among gaps>55
    pings_needed = int(sleep_min/55)+1  # a ping every ~55min to stay under TTL
    ka_total = pings_needed*keepalive(pct)
    # then user returns to WARM full ctx → resume cheap
    warm_resume = tok(pct)*READ + AVG_OUT_TURN*OUT
    ka_strategy = ka_total + warm_resume
    compact_strategy = compact_now(pct) + cold_start_after_compact()
    print(f"ctx={pct}% sleep~{sleep_min}min: keep-alive×{pings_needed}=${ka_strategy*1000:.0f}m (keeps ctx) "
          f"vs compact-before-sleep=${compact_strategy*1000:.0f}m (loses ctx→summary)")
print("\n(keep-alive keeps full context but pays N pings reading full ctx; compact pays once, tiny after)")
