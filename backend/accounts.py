from pydantic import BaseModel
import json
from dotenv import load_dotenv
from datetime import datetime
from .database import write_account, read_account, write_log


load_dotenv(override=True)

INITIAL_BALANCE = 10_000.0
SPREAD = 0.002


class Transaction(BaseModel):
    symbol: str
    quantity: int
    price: float
    timestamp: str
    rationale: str

    def total(self) -> float:
        return self.quantity * self.price

    def __repr__(self):
        return f"{abs(self.quantity)} shares of {self.symbol} at {self.price} each."


class Account(BaseModel):
    name: str
    balance: float
    strategy: str
    holdings: dict[str, int]
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]

    @classmethod
    def get(cls, name: str):
        fields = read_account(name.lower())
        if not fields:
            fields = {
                "name": name.lower(),
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": []
            }
            write_account(name, fields)
        return cls(**fields)

    def save(self):
        write_account(self.name.lower(), self.model_dump())

    def reset(self, strategy: str = ""):
        self.balance = INITIAL_BALANCE
        self.strategy = strategy
        self.holdings = {}
        self.transactions = []
        self.portfolio_value_time_series = []
        self.save()
