# Binance-based FastAPI Backend

from fastapi import FastAPI
from pydantic import BaseModel
import os

# Constants
BINANCE_BASE = os.getenv('BINANCE_BASE')
BINANCE_SYMBOLS = os.getenv('BINANCE_SYMBOLS')

app = FastAPI()

class Order(BaseModel):
    price: float
    volume: float

@app.post('/trade/')
def create_trade(order: Order):
    return {'price': order.price, 'volume': order.volume, 'status': 'executed'}
