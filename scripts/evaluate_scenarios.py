import json,statistics
from pathlib import Path
from dataclasses import replace
from loadshift.fixtures import COMPARISON_WINDOW,load_forecast_day,load_demo_tasks
from loadshift.comparison import compare_day
from loadshift.tasks import TimeWindow
scenarios=[('default',1,None),('half_solar',.5,None),('more_solar',1.5,None),('evening_only',1,TimeWindow('00:00','17:00'))]
result=[]
for name,scale,window in scenarios:
 rows=[]
 for date in COMPARISON_WINDOW:
  day=load_forecast_day(date=date);day.frame['pv_w']*=scale
  jobs=load_demo_tasks()
  if window: jobs=[replace(t,inconvenient_windows=(*t.inconvenient_windows,window)) for t in jobs]
  rows.append(compare_day(date,tasks=jobs,day=day))
 scored=[r for r in rows if r['loadshift_versus_tariff_greedy'] is not None]
 result.append({'scenario':name,'solar_scale':scale,'availability':'17:00 onward' if window else 'default','n':len(rows),
   'median_saving_vs_original':statistics.median(-r['loadshift_versus_original']['bill_usd'] for r in scored),
   'median_advantage_vs_greedy':statistics.median(-r['loadshift_versus_tariff_greedy']['bill_usd'] for r in scored),
   'outcomes':{k:sum(r['verdict_vs_tariff_greedy']==k for r in rows) for k in ['win','tie','lose']},'days':rows})
p=Path('artifacts/evaluation.json');p.write_text(json.dumps({'window':list(COMPARISON_WINDOW),'scenario_selection':'Four fixed scenarios: base, half solar, 1.5x solar, evening-only. All 14 days retained in each.','scenarios':result},indent=2)+'\n')
for r in result:print({k:v for k,v in r.items() if k!='days'})
