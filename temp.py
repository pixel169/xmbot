import MetaTrader5 as mt5
import config

mt5.initialize(login=int(config.MT5_LOGIN),
               password=config.MT5_PASSWORD,
               server=config.MT5_SERVER)

groups = {}
for s in mt5.symbols_get():
    top = (s.path or "").split("\\", 1)[0]   # top-level group
    groups.setdefault(top, []).append(s.name)

for g in sorted(groups):
    print(f"{g:25} {len(groups[g])} symbols  e.g. {groups[g][:3]}")

mt5.shutdown()
