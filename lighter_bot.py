import asyncio
import aiohttp
import json
import hmac
import hashlib
import time
import random
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import logging
from datetime import datetime, timedelta
import uuid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("trading_bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"

@dataclass
class AccountConfig:
    account_name: str
    api_key: str
    secret_key: str

@dataclass
class TradingConfig:
    base_url: str = "https://api.lighter.xyz"
    request_timeout: int = 30
    max_retries: int = 3
    min_session_duration: int = 300  # 5 minutes in seconds
    max_session_duration: int = 2100  # 35 minutes in seconds
    min_daily_sessions: int = 70
    max_daily_sessions: int = 180
    max_position_size: float = 0.1  # Maximum position size in ETH or other base currency
    symbols: List[str] = None  # Will be populated from API

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = ["ETH-USDC", "BTC-USDC"]  # Default symbols

@dataclass
class Session:
    session_id: str
    symbol: str
    account1_long: bool
    start_time: datetime
    planned_duration: int
    end_time: Optional[datetime] = None
    account1_order_id: Optional[str] = None
    account2_order_id: Optional[str] = None
    closed: bool = False

class LighterTradingBot:
    def __init__(self, account_configs: List[AccountConfig], trading_config: TradingConfig):
        if len(account_configs) != 2:
            raise ValueError("Exactly two account configs are required for delta neutral strategy")
        
        self.account_configs = account_configs
        self.trading_config = trading_config
        self.sessions: List[Session] = []
        self.active_session: Optional[Session] = None
        self.session_task: Optional[asyncio.Task] = None
        self.is_running = False
        
        # Create a session for each account
        self.sessions_per_account = [
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.trading_config.request_timeout))
            for _ in range(2)
        ]
        
        logger.info("LighterTradingBot initialized with delta neutral strategy")

    async def close(self):
        """Clean up resources"""
        for session in self.sessions_per_account:
            await session.close()
        logger.info("LighterTradingBot shutdown complete")

    def _generate_signature(self, secret_key: str, payload: str) -> str:
        """Generate HMAC-SHA256 signature"""
        return hmac.new(
            secret_key.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def _get_nonce(self) -> int:
        """Generate unique nonce for each request"""
        return int(time.time() * 1000)

    async def _make_request(self, account_index: int, method: str, endpoint: str, **kwargs) -> Dict:
        """Make authenticated API request with retry logic"""
        account_config = self.account_configs[account_index]
        headers = {
            "X-API-KEY": account_config.api_key,
            "Content-Type": "application/json"
        }

        # Add authentication if needed
        if not endpoint.startswith("/public"):
            nonce = self._get_nonce()
            signature_payload = f"{nonce}{method}{endpoint}"
            signature = self._generate_signature(account_config.secret_key, signature_payload)
            headers.update({
                "X-NONCE": str(nonce),
                "X-SIGNATURE": signature
            })

        url = f"{self.trading_config.base_url}{endpoint}"

        for attempt in range(self.trading_config.max_retries):
            try:
                async with self.sessions_per_account[account_index].request(
                    method, url, headers=headers, **kwargs
                ) as response:
                    data = await response.json()

                    if response.status != 200:
                        logger.error(f"API error {response.status} for account {account_index}: {data}")
                        raise Exception(f"API error: {data}")

                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Request failed (attempt {attempt + 1}) for account {account_index}: {str(e)}")
                if attempt == self.trading_config.max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)  # Exponential backoff

    async def get_market_data(self, symbol: str) -> Dict:
        """Get current market data for a symbol"""
        endpoint = f"/public/markets/{symbol}"
        return await self._make_request(0, "GET", endpoint)  # Use first account for public data

    async def get_balance(self, account_index: int) -> Dict:
        """Get user account balance"""
        endpoint = "/private/account/balance"
        return await self._make_request(account_index, "GET", endpoint)

    async def place_order(self, account_index: int, symbol: str, side: OrderSide, 
                         order_type: OrderType, quantity: float, price: Optional[float] = None) -> Dict:
        """Place a new order"""
        endpoint = "/private/orders"

        order_data = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "quantity": str(quantity),
        }

        if price is not None:
            order_data["price"] = str(price)

        return await self._make_request(account_index, "POST", endpoint, json=order_data)

    async def cancel_order(self, account_index: int, order_id: str) -> Dict:
        """Cancel an existing order"""
        endpoint = f"/private/orders/{order_id}"
        return await self._make_request(account_index, "DELETE", endpoint)

    async def get_order_status(self, account_index: int, order_id: str) -> Dict:
        """Get order status"""
        endpoint = f"/private/orders/{order_id}"
        return await self._make_request(account_index, "GET", endpoint)

    async def get_open_orders(self, account_index: int, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open orders"""
        endpoint = "/private/orders/open"
        params = {"symbol": symbol} if symbol else {}
        response = await self._make_request(account_index, "GET", endpoint, params=params)
        return response.get("orders", [])

    async def close_all_orders(self, account_index: int, symbol: str):
        """Close all open orders for a symbol"""
        open_orders = await self.get_open_orders(account_index, symbol)
        for order in open_orders:
            try:
                await self.cancel_order(account_index, order['id'])
                logger.info(f"Cancelled order {order['id']} for account {account_index}")
            except Exception as e:
                logger.error(f"Failed to cancel order {order['id']} for account {account_index}: {str(e)}")

    async def execute_delta_neutral_session(self):
        """Execute a delta neutral trading session"""
        if self.active_session:
            logger.warning("A session is already active")
            return

        # Choose random parameters for this session
        symbol = random.choice(self.trading_config.symbols)
        account1_long = random.choice([True, False])
        duration = random.randint(
            self.trading_config.min_session_duration,
            self.trading_config.max_session_duration
        )
        
        # Generate a unique session ID
        session_id = str(uuid.uuid4())[:8]
        
        # Create session object
        session = Session(
            session_id=session_id,
            symbol=symbol,
            account1_long=account1_long,
            start_time=datetime.now(),
            planned_duration=duration
        )
        
        self.active_session = session
        self.sessions.append(session)
        
        logger.info(f"Starting delta neutral session {session_id} on {symbol}, "
                   f"Account1 {'long' if account1_long else 'short'}, "
                   f"Duration: {duration}s")
        
        try:
            # Get current market price
            market_data = await self.get_market_data(symbol)
            current_price = float(market_data['last_price'])
            
            # Determine position size (random but within limits)
            position_size = random.uniform(
                self.trading_config.max_position_size * 0.3,
                self.trading_config.max_position_size
            )
            
            # Place opposing orders
            if account1_long:
                # Account 1 goes long, Account 2 goes short
                order1 = await self.place_order(
                    0, symbol, OrderSide.BUY, OrderType.MARKET, position_size
                )
                order2 = await self.place_order(
                    1, symbol, OrderSide.SELL, OrderType.MARKET, position_size
                )
            else:
                # Account 1 goes short, Account 2 goes long
                order1 = await self.place_order(
                    0, symbol, OrderSide.SELL, OrderType.MARKET, position_size
                )
                order2 = await self.place_order(
                    1, symbol, OrderSide.BUY, OrderType.MARKET, position_size
                )
            
            session.account1_order_id = order1.get('id')
            session.account2_order_id = order2.get('id')
            
            logger.info(f"Session {session_id}: Opened positions - "
                       f"Account1: {order1['id']}, Account2: {order2['id']}, "
                       f"Size: {position_size}, Price: {current_price}")
            
            # Wait for the planned duration
            await asyncio.sleep(duration)
            
            # Close positions in opposite direction
            if account1_long:
                # Account 1 sells, Account 2 buys to close
                close_order1 = await self.place_order(
                    0, symbol, OrderSide.SELL, OrderType.MARKET, position_size
                )
                close_order2 = await self.place_order(
                    1, symbol, OrderSide.BUY, OrderType.MARKET, position_size
                )
            else:
                # Account 1 buys, Account 2 sells to close
                close_order1 = await self.place_order(
                    0, symbol, OrderSide.BUY, OrderType.MARKET, position_size
                )
                close_order2 = await self.place_order(
                    1, symbol, OrderSide.SELL, OrderType.MARKET, position_size
                )
            
            # Update session info
            session.end_time = datetime.now()
            session.closed = True
            
            # Get closing price
            market_data = await self.get_market_data(symbol)
            closing_price = float(market_data['last_price'])
            
            # Calculate PnL (should be approximately zero due to delta neutrality)
            price_change_pct = (closing_price - current_price) / current_price
            if account1_long:
                account1_pnl = position_size * price_change_pct
                account2_pnl = -position_size * price_change_pct
            else:
                account1_pnl = -position_size * price_change_pct
                account2_pnl = position_size * price_change_pct
            
            total_pnl = account1_pnl + account2_pnl
            
            logger.info(f"Session {session_id}: Closed positions - "
                       f"Account1 PnL: {account1_pnl:.4f}, "
                       f"Account2 PnL: {account2_pnl:.4f}, "
                       f"Total PnL: {total_pnl:.4f}, "
                       f"Duration: {duration}s")
            
        except Exception as e:
            logger.error(f"Session {session_id} failed: {str(e)}")
            # Attempt to close any open positions
            try:
                await self.close_all_orders(0, symbol)
                await self.close_all_orders(1, symbol)
            except Exception as close_error:
                logger.error(f"Failed to close positions after error: {str(close_error)}")
        finally:
            self.active_session = None

    async def run_daily_cycle(self):
        """Run the daily cycle of trading sessions"""
        self.is_running = True
        
        # Calculate number of sessions for today
        daily_sessions = random.randint(
            self.trading_config.min_daily_sessions,
            self.trading_config.max_daily_sessions
        )
        
        logger.info(f"Starting daily cycle with {daily_sessions} sessions")
        
        # Calculate minimum delay between sessions (spread out over 24 hours)
        min_delay = (24 * 3600) / daily_sessions
        
        for session_num in range(daily_sessions):
            if not self.is_running:
                break
                
            # Execute a session
            try:
                await self.execute_delta_neutral_session()
            except Exception as e:
                logger.error(f"Session {session_num + 1} failed: {str(e)}")
            
            # Calculate delay until next session (randomized but ensuring we fit all sessions)
            delay = random.uniform(min_delay * 0.7, min_delay * 1.3)
            logger.info(f"Completed session {session_num + 1}/{daily_sessions}. Next session in {delay/60:.1f} minutes")
            
            # Wait for next session
            await asyncio.sleep(delay)
        
        logger.info("Daily cycle completed")

    def stop(self):
        """Stop the trading bot"""
        self.is_running = False
        logger.info("Trading bot stopping...")

async def main():
    """Main trading bot execution"""
    # Configuration - Replace with your actual API keys
    account_configs = [
        AccountConfig(
            account_name="Account1",
            api_key="your_account1_api_key_here",
            secret_key="your_account1_secret_key_here"
        ),
        AccountConfig(
            account_name="Account2",
            api_key="your_account2_api_key_here",
            secret_key="your_account2_secret_key_here"
        )
    ]
    
    trading_config = TradingConfig()
    
    # Initialize bot
    bot = LighterTradingBot(account_configs, trading_config)
    
    try:
        # Check balances before starting
        for i, account in enumerate(account_configs):
            balance = await bot.get_balance(i)
            logger.info(f"{account.account_name} balance: {balance}")
        
        # Run the daily cycle
        await bot.run_daily_cycle()
        
    except Exception as e:
        logger.error(f"Trading bot error: {str(e)}")
        raise
    finally:
        await bot.close()

if __name__ == "__main__":
    # Run the bot
    asyncio.run(main())
