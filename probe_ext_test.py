import sys, json
sys.path.insert(0, 'backend')
from app.services.market_data import market_data_provider
snap = market_data_provider.get_external_snapshot()
print(json.dumps(snap, ensure_ascii=False, indent=2, default=str)[:1600])
