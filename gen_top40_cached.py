import json, sys
sys.stdout.reconfigure(encoding='utf-8')
pilot=json.load(open('output/backtest_pilot.json',encoding='utf-8'))
uni=json.load(open('output/backtest_universe.json',encoding='utf-8'))
by_ex=uni['by_ex']
vols=pilot['vols']
# filter to those on okx,bitget,mexc,bingx
exs=['okx','bitget','mexc','bingx']
cand=[]
for s,med in vols.items():
    if not all(s in by_ex.get(ex,[]) for ex in exs):
        continue
    if 100000 <= med <= 10000000:
        cand.append((s,med))
cand=sorted(cand, key=lambda x: x[1], reverse=True)[:40]
print('picked',len(cand))
for s,v in cand:
    print(s, int(v))
open('output/top40_4ex.txt','w').write('\n'.join(s for s,_ in cand))
