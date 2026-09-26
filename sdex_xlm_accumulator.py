import os
import time
import json
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset

# --- CONFIGURATION ---
SECRET_KEY = os.getenv("STELLAR_SECRET_KEY")
kp = Keypair.from_secret(SECRET_KEY)
public_key = kp.public_key

server = Server("https://horizon.stellar.org")
XLM = Asset.native()
USDC = Asset("USDC", "GA5ZSEJYB37JRC5AVCIA5XYG4DZ6N21555WKHD745E21A562PXRTHRHU")

STATE_FILE = "grid_state.json"
NUM_TIERS = 10             # Divide available capital into 10 equal parts
MIN_TRADE_USDC = 1.0       # Minimum trade size floor ($1.00)
PROFIT_MARGIN = 1.020      # +2.0% profit target per tier
MAX_OFFER_AGE_HOURS = 24.0 # Auto-cancel limit orders older than 24h

# --- STATE MANAGEMENT ---
def loadHere is how to modify the calculation logic so it automatically splits whatever total balance you have into 10 equal trades.

### The Calculation Logic

Divide your available balance by 10 to determine the trade size, while keeping each individual trade logic identical:

```python
# 1. Get total available balance
total_balance = get_available_balance()  # e.g., 10.00 or 100.00

# 2. Divide by total desired trades (10)
num_trades = 10
trade_amount = total_balance / num_trades  # Yields $1.00 for $10, or $10.00 for $100

# 3. Execute the loop
for i in range(num_trades):
    create_trade(amount=trade_amount)
