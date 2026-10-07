"""Signal values retained for the existing browser execution interface."""

from enum import Enum


class Signal(Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
