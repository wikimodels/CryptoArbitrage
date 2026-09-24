import sys
sys.stdout.reconfigure(encoding='utf-8')
import yaml, statistics, json
from cryptoarb.backtest.universe import build_universe, fetch_volumes
cfg=yaml.safe_load(open('cryptoarb/backtest/screen.yaml',encoding='utf-8'))
cfg['pilot_exchanges']=['okx','bitget','mexc','bingx']
common, by_ex = build_universe(cfg['exchanges'], cfg['min_common_exchanges'])
print('common',len(common))
vols={}
for ex in ['okx','bitget','mexc','bingx']:
    vols[ex]=fetch_volumes(ex)
    print(ex, len(vols[ex]))
picked=[]
for s in common:
    if not all(s in by_ex.get(ex,[]) for ex in ['okx','bitget','mexc','bingx']):
        continue
    vs=[vols[ex].get(s,0) for ex in ['okx','bitget','mexc','bingx'] if vols[ex].get(s)]
    if len(vs)<4: continue
    med=statistics.median(vs)
    if 100000 <= med <= 10000000:
        picked.append((s,med))
picked=sorted(picked, key=lambda x: x[1], reverse=True)[:40]
print('picked',len(picked))
for s,v in picked:
    print(s, int(v))
open('output/top40_4ex.txt','w').write('\n'.join(s for s,_ in picked))
