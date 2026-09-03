BASE=5/1e6; OUT=25/1e6; READ=0.1*BASE; WRITE=2.0*BASE; AVG=338; CTX=200000
SUM_OUT=2280; CTX_AFTER=0.04*CTX; P=35
def tok(p): return p/100*CTX
compact = tok(P)*READ + SUM_OUT*OUT + CTX_AFTER*WRITE
cold = tok(P)*WRITE + AVG*OUT
cold4 = CTX_AFTER*WRITE + AVG*OUT
print(f"compact at 35pct = {compact*1000:.0f}m (read {tok(P)*READ*1000:.0f} + gen {SUM_OUT*OUT*1000:.0f} + write4pct {CTX_AFTER*WRITE*1000:.0f})")
print(f"cold={cold*1000:.0f}m after-compact={cold4*1000:.0f}m save/cold={(cold-cold4)*1000:.0f}m ({cold/cold4:.1f}x)")
fired=153; false=0.075; real=fired*(1-false)
gross=real*(cold-cold4); spend=fired*compact; net=gross-spend
print(f"monthly: gross ${gross:.1f} - compact spend ${spend:.1f} = NET ${net:.1f} (weekly ${net/30*7:.1f})")
print(f"net pct Kesha burn(333): {100*net/333:.1f}  |  pct of 200 Max: {100*net/200:.1f}")
cold_total=141*cold
print(f"cold burn WITHOUT compact: ${cold_total:.0f}/mo = {100*cold_total/333:.0f}pct of Kesha 333")
print(f"cold-start share: {100*cold_total/333:.0f}pct -> {100*(cold_total-net)/333:.0f}pct")
