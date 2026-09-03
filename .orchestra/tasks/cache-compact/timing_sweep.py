"""Optimal compact-timing sweep: EV of net benefit vs do-nothing, at each idle minute T.
At 40% ctx (Kesha typical). Uses measured conditional P(gap>60|gap>T)."""
CTX=200_000; BASE=5/1e6; OUT=25/1e6; READ=0.1*BASE; WRITE=2.0*BASE
AVG_OUT=338; SUMMARY=2280
def tok(p): return p/100*CTX
def cold(p): return tok(p)*WRITE + AVG_OUT*OUT
def compact(p): return tok(p)*READ + SUMMARY*OUT + SUMMARY*WRITE
def cold_small(): return SUMMARY*WRITE + AVG_OUT*OUT
def warm(p): return tok(p)*READ + AVG_OUT*OUT      # warm resume (cache hit)
def warm_small(): return SUMMARY*READ + AVG_OUT*OUT

# measured P(gap>60 | gap>T)
P={1:.118,5:.246,10:.338,15:.417,20:.487,25:.534,30:.607,35:.669,40:.740,45:.787,50:.841,55:.925,59:.991}
PCT=40
print(f"ctx={PCT}%. EV(net benefit of compacting at minute T) vs do-nothing.")
print(f"{'T':>4} {'P(cold|>T)':>11} {'EV_donothing':>13} {'EV_compact':>11} {'net benefit':>12} {'compact?':>9}")
best=(None,-1)
for T in sorted(P):
    p=P[T]
    # do nothing: p*cold(full) + (1-p)*warm(full)
    ev_do = p*cold(PCT) + (1-p)*warm(PCT)
    # compact now: pay compact + p*cold_small + (1-p)*warm_small
    ev_comp = compact(PCT) + p*cold_small() + (1-p)*warm_small()
    net = ev_do - ev_comp   # positive = compacting saves this much (milli-$)
    if net>best[1]: best=(T,net)
    print(f"{T:>4} {p*100:>9.1f}% ${ev_do*1000:>11.2f}m ${ev_comp*1000:>9.2f}m ${net*1000:>10.2f}m {'YES' if net>0 else 'no':>9}")
print(f"\nMax net benefit at T={best[0]}min (${best[1]*1000:.1f}m saved). But note: net RISES with T monotonically")
print("because higher P = fewer wasted compacts. The 'cost' of waiting is the RISK the cache evicts")
print("BEFORE you compact (gap could jump past 60 between your check intervals).")
# The real tradeoff: if you wait too long, cache may already be cold when you compact → compact expensive
print("\n=== If you check every 5min and gap lands in [T, T+5], risk cache already evicted if T>=60 ===")
print("Practical sweet spot = latest T still safely < TTL. With TTL~60, T=50-55 maximizes P while")
print("keeping compact on a WARM cache. T<30 → too many false compacts (P<60%).")

print("\n=== EV with CORRECT eviction model: TTL=60min (Anthropic docs + measured, reset-on-hit) ===")
# CORRECTION: prior version used an invented linear 30->60 ramp — NOT supported by data.
# Primary research (docs/tasks/cache-optimization/research.md, measured): cost FLAT 0-30min
# (0.82x at 18-30 = still cached); the noisy 30-60 bucket (n=10, 1.12x) is a TTL-reset artifact,
# not eviction. Docs unambiguous: eviction AFTER 60min, resets on every hit.
# → P(cache evicted at T) = 0 for T<60, 1 for T>=60. Compact at any T<60 runs on a WARM cache.
def p_evicted(T):
    return 0.0 if T < 60 else 1.0
def compact_cold(p_pct):  # compact when cache already evicted → reads at create
    return tok(p_pct)*WRITE + SUMMARY*OUT + SUMMARY*WRITE
print(f"{'T':>4} {'P(cold|>T)':>11} {'P(evicted@T)':>12} {'EV_net_adj':>11}")
best=(None,-1)
for T in sorted(P):
    p=P[T]; pe=p_evicted(T)
    ev_do = p*cold(PCT)+(1-p)*warm(PCT)
    # compact cost blends warm-compact and (if already evicted) cold-compact
    comp_cost = (1-pe)*compact(PCT) + pe*compact_cold(PCT)
    ev_comp = comp_cost + p*cold_small() + (1-p)*warm_small()
    net = ev_do - ev_comp
    if net>best[1]: best=(T,net)
    print(f"{T:>4} {p*100:>9.1f}% {pe*100:>10.0f}% ${net*1000:>9.1f}m")
print(f"\nEV rises monotonically to T={best[0]}min (P(evicted)=0 for all T<60). Optimal fire ≈ 55-59min:")
print("latest safe moment before TTL — maximizes P(user gone) while cache still warm. Confirms user intuition.")
