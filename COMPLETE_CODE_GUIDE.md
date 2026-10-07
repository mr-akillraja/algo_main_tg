# Historical execution reference

The old strategy instructions were removed. See [STRATEGY_GUIDE.md](STRATEGY_GUIDE.md)
for Part B and the current feed limitations. The following original execution
excerpt is retained verbatim as historical text; it is not an implementation
instruction or a supported entry point.

```python
    # Execute
    mode    = "ftt" if settings.TRADE_MODE in ("ftt", "both") else settings.TRADE_MODE
    success = await self.supervisor.safe_execute_trade(final_signal, final_conf, mode)
    log.info(f"[Supervisor] Trade placed={success}")
```
