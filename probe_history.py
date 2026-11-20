import requests, json, re, datetime, warnings
warnings.filterwarnings('ignore')
proxies={'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897'}
for tag in ['mlb','tennis','sports']:
    params={'limit':100,'closed':'true','tag_slug':tag,'order':'endDate','ascending':'false','end_date_min':'2026-09-04T00:00:00Z','end_date_max':'2026-09-06T00:00:00Z'}
    r=requests.get('https://gamma-api.polymarket.com/events',params=params,proxies=proxies,verify=False,timeout=30)
    data=r.json()
    print('\nTAG',tag,'status',r.status_code,'count',len(data) if isinstance(data,list) else type(data))
    for e in data:
        title=e.get('title','')
        if any(x in title for x in ['Tommy Paul','ATL','MIL','TOR','LAD']):
            print(json.dumps({'id':e.get('id'),'title':title,'slug':e.get('slug'),'closed':e.get('closed'),'endDate':e.get('endDate'),'markets':[{'id':m.get('id'),'question':m.get('question'),'outcomes':m.get('outcomes'),'outcomePrices':m.get('outcomePrices'),'closed':m.get('closed'),'closedTime':m.get('closedTime'),'clobTokenIds':m.get('clobTokenIds')} for m in e.get('markets',[])][:3]},ensure_ascii=False)[:3000])
