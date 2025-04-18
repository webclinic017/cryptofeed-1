'''
Copyright (C) 2017-2025 Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''
from collections import defaultdict
import logging
from decimal import Decimal
from typing import Dict, List, Tuple, Union
import base64
import hashlib
import hmac
import time
import itertools

from yapic import json

from cryptofeed.connection import AsyncConnection, RestEndpoint, Routes, WebsocketEndpoint
from cryptofeed.defines import BID, ASK, BUY, CANCELLED, FAILED, FILLED, GEMINI, L2_BOOK, LIMIT, OPEN, SELL, STOP_LIMIT, SUBMITTING, TRADES, ORDER_INFO
from cryptofeed.feed import Feed
from cryptofeed.symbols import Symbol
from cryptofeed.exchanges.mixins.gemini_rest import GeminiRestMixin
from cryptofeed.types import OrderBook, Trade, OrderInfo


LOG = logging.getLogger('feedhandler')


class Gemini(Feed, GeminiRestMixin):
    id = GEMINI
    websocket_channels = {
        L2_BOOK: L2_BOOK,
        TRADES: TRADES,
        ORDER_INFO: ORDER_INFO
    }
    websocket_endpoints = [
        WebsocketEndpoint('wss://api.gemini.com/v2/marketdata/', sandbox='wss://api.sandbox.gemini.com/v2/marketdata/', channel_filter=[websocket_channels[L2_BOOK], websocket_channels[TRADES]]),
        WebsocketEndpoint('wss://api.gemini.com/v1/order/events', sandbox='wss://api.sandbox.gemini.com/v1/order/events', channel_filter=[websocket_channels[ORDER_INFO]], authentication=True)
    ]
    rest_endpoints = [RestEndpoint('https://api.gemini.com', routes=Routes('/v1/symbols/details/{}', currencies='/v1/symbols', authentication='/v1/order/events'))]
    request_limit = 1

    @classmethod
    def timestamp_normalize(cls, ts: float) -> float:
        return ts / 1000.0

    @classmethod
    def _symbol_endpoint_prepare(cls, ep: RestEndpoint) -> Union[List[str], str]:
        ret = cls.http_sync.read(ep.route('currencies'), json=True, uuid=cls.id)
        return [ep.route('instruments').format(currency) for currency in ret]

    @classmethod
    def _parse_symbol_data(cls, data: dict) -> Tuple[Dict, Dict]:
        ret = {}
        info = defaultdict(dict)

        for symbol in data:
            if symbol['status'] == 'closed':
                continue
            s = Symbol(symbol['base_currency'], symbol['quote_currency'])
            ret[s.normalized] = symbol['symbol']
            info['tick_size'][s.normalized] = symbol['tick_size']
            info['instrument_type'][s.normalized] = s.type
        return ret, info

    def __reset(self, pairs):
        for pair in pairs:
            self._l2_book[self.exchange_symbol_to_std_symbol(pair)] = OrderBook(self.id, self.exchange_symbol_to_std_symbol(pair), max_depth=self.max_depth)

    def generate_token(self, payload=None) -> dict:
        if not payload:
            payload = {}
        payload['request'] = self.rest_endpoints[0].routes.authentication
        payload['nonce'] = int(time.time() * 1000)

        if self.account_name:
            payload['account'] = self.account_name

        b64_payload = base64.b64encode(json.dumps(payload).encode('utf-8'))
        signature = hmac.new(self.key_secret.encode('utf-8'), b64_payload, hashlib.sha384).hexdigest()

        return {
            'X-GEMINI-PAYLOAD': b64_payload.decode(),
            'X-GEMINI-APIKEY': self.key_id,
            'X-GEMINI-SIGNATURE': signature
        }

    async def _book(self, msg: dict, timestamp: float):
        pair = self.exchange_symbol_to_std_symbol(msg['symbol'])
        # Gemini sends ALL data for the symbol, so if we don't actually want
        # the book data, bail before parsing
        if self.subscription and ((L2_BOOK in self.subscription and msg['symbol'] not in self.subscription[L2_BOOK]) or L2_BOOK not in self.subscription):
            return

        data = msg['changes']
        forced = not len(self._l2_book[pair].book.bids)
        delta = {BID: [], ASK: []}
        for entry in data:
            side = ASK if entry[0] == 'sell' else BID
            price = Decimal(entry[1])
            amount = Decimal(entry[2])
            if amount == 0:
                if price in self._l2_book[pair].book[side]:
                    del self._l2_book[pair].book[side][price]
                    delta[side].append((price, 0))
            else:
                self._l2_book[pair].book[side][price] = amount
                delta[side].append((price, amount))

        await self.book_callback(L2_BOOK, self._l2_book[pair], timestamp, delta=delta if not forced else None, raw=msg)

    async def _trade(self, msg: dict, timestamp: float):
        pair = self.exchange_symbol_to_std_symbol(msg['symbol'])
        price = Decimal(msg['price'])
        side = SELL if msg['side'] == 'sell' else BUY
        amount = Decimal(msg['quantity'])
        t = Trade(self.id, pair, side, amount, price, self.timestamp_normalize(msg['timestamp']), id=str(msg['event_id']), raw=msg)
        await self.callback(TRADES, t, timestamp)

    async def _order(self, msg: dict, timestamp: float):
        '''
        [{
            "type": "accepted",
            "order_id": "109535951",
            "event_id": "109535952",
            "api_session": "UI",
            "symbol": "btcusd",
            "side": "buy",
            "order_type": "exchange limit",
            "timestamp": "1547742904",
            "timestampms": 1547742904989,
            "is_live": true,
            "is_cancelled": false,
            "is_hidden": false,
            "original_amount": "1",
            "price": "3592.00",
            "socket_sequence": 13
        }]
        '''
        if msg['type'] == "initial" or msg['type'] == "accepted":
            status = SUBMITTING
        elif msg['type'] == "fill":
            status = FILLED
        elif msg['type'] == 'booked':
            status = OPEN
        elif msg['type'] == 'rejected':
            status = FAILED
        elif msg['type'] == 'cancelled':
            status = CANCELLED
        else:
            status = msg['type']

        oi = OrderInfo(
            self.id,
            self.exchange_symbol_to_std_symbol(msg['symbol'].upper()),
            msg['order_id'],
            BUY if msg['side'].lower() == 'buy' else SELL,
            status,
            LIMIT if msg['order_type'] == 'exchange limit' else STOP_LIMIT,
            Decimal(msg['price']),
            Decimal(msg['executed_amount']),
            Decimal(msg['remaining_amount']),
            msg['timestampms'] / 1000.0,
            raw=msg
        )
        await self.callback(ORDER_INFO, oi, timestamp)

    async def message_handler(self, msg: str, conn: AsyncConnection, timestamp: float):
        msg = json.loads(msg, parse_float=Decimal)

        if isinstance(msg, list):
            for entry in msg:
                await self._order(entry, timestamp)
            return

        if 'type' not in msg:
            LOG.warning('%s: Error from exchange %s', self.id, msg)
        elif msg['type'] == 'l2_updates':
            await self._book(msg, timestamp)
        elif msg['type'] == 'trade':
            await self._trade(msg, timestamp)
        elif msg['type'] == 'heartbeat':
            return
        elif msg['type'] == 'subscription_ack':
            LOG.info('%s: Authenticated successfully', self.id)
        elif msg['type'] == 'auction_result' or msg['type'] == 'auction_indicative' or msg['type'] == 'auction_open':
            return
        else:
            LOG.warning('%s: Invalid message type %s', self.id, msg)

    async def _ws_authentication(self, address: str, options: dict) -> Tuple[str, dict]:
        header = self.generate_token()
        symbols = []
        for channel in self.subscription:
            if self.is_authenticated_channel(channel):
                symbols.extend(self.subscription.get(channel))
        symbols = '&'.join([f"symbolFilter={s.lower()}" for s in symbols])  # needs to match REST format (lower case)
        options['extra_headers'] = header
        return f'{address}?{symbols}', options

    async def subscribe(self, conn: AsyncConnection):
        if self.std_channel_to_exchange(ORDER_INFO) in conn.subscription:
            return

        symbols = list(set(itertools.chain(*conn.subscription.values())))
        self.__reset(symbols)
        await conn.write(json.dumps({"type": "subscribe", "subscriptions": [{"name": "l2", "symbols": symbols}]}))
