# Kraken-based FastAPI Backend

from fastapi import FastAPI
from pydantic import BaseModel
import os

# Constants
KRAKEN_BASE = os.getenv('KRAKEN_BASE')
KRAKEN_SYMBOLS = os.getenv('KRAKEN_SYMBOLS')

app = FastAPI()

class Order(BaseModel):
    price: float
    volume: float

@app.post('/trade/')
def create_trade(order: Order):
    return {'price': order.price, 'volume': order.volume, 'status': 'executed'}
