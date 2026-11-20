from datetime import datetime, timezone
ids=[('182719',1788551705.102883,'Tommy Paul'),('401816798',1788570233.9495604,'ATL'),('401816802',1788573349.1370015,'MIL'),('401816807',1788574727.4336064,'TOR'),('401816811',1788582786.8279643,'LAD')]
for game_id, ts, team in ids:
    print(game_id, team, datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(), datetime.fromtimestamp(ts, timezone.utc).strftime('%Y%m%d'))
