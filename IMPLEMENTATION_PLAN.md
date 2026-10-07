# Historical execution reference

The old strategy instructions were removed. See [STRATEGY_GUIDE.md](STRATEGY_GUIDE.md)
for Part B and the current feed limitations. The following original execution
excerpt is retained verbatim as historical text; it is not an implementation
instruction or a supported entry point.

```python
# In bot/browser.py or similar

class OlymtradeBot:
    async def execute_trade(self, direction, amount, duration):
        """Execute actual trade with proper position sizing."""
        # Enforce risk limits
        if amount > settings.RISK_PER_TRADE:
            log.warning(f"Position size {amount} exceeds limit {settings.RISK_PER_TRADE}")
            amount = settings.RISK_PER_TRADE
        
        # Execute with framework validation
        return await self._send_trade_request(direction, amount, duration)
```
