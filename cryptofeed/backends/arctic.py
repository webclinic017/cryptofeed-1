'''
Copyright (C) 2017-2025 Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.

Book backends are intentionally left out here - Arctic cannot handle high throughput
data like book data. Arctic is best used for writing large datasets in batches.
'''
import arctic
import pandas as pd

from cryptofeed.backends.backend import BackendCallback
from cryptofeed.defines import BALANCES, CANDLES, FILLS, FUNDING, OPEN_INTEREST, ORDER_INFO, TICKER, TRADES, LIQUIDATIONS, TRANSACTIONS


class ArcticCallback:
    def __init__(self, library, host='127.0.0.1', key=None, none_to=None, numeric_type=float, quota=0, ssl=False, **kwargs):
        """
        library: str
            arctic library. Will be created if does not exist.
        key: str
            setting key lets you override the symbol name.
            The defaults are related to the data
            being stored, i.e. trade, funding, etc
        quota: int
            absolute number of bytes that this library is limited to.
            The default of 0 means that the storage size is unlimited.
        kwargs:
            if library needs to be created you can specify the
            lib_type in the kwargs. Default is VersionStore, but you can
            set to chunkstore with lib_type=arctic.CHUNK_STORE
        """
        con = arctic.Arctic(host, ssl=ssl)
        if library not in con.list_libraries():
            lib_type = kwargs.get('lib_type', arctic.VERSION_STORE)
            con.initialize_library(library, lib_type=lib_type)
        con.set_quota(library, quota)
        self.lib = con[library]
        self.key = key if key else self.default_key
        self.numeric_type = numeric_type
        self.none_to = none_to

    async def write(self, data):
        df = pd.DataFrame({key: [value] for key, value in data.items()})
        df['date'] = pd.to_datetime(df.timestamp, unit='s')
        df['receipt_timestamp'] = pd.to_datetime(df.receipt_timestamp, unit='s')
        df.set_index(['date'], inplace=True)
        if 'type' in df and df.type.isna().any():
            df.drop(columns=['type'], inplace=True)
        df.drop(columns=['timestamp'], inplace=True)
        self.lib.append(self.key, df, upsert=True)


class TradeArctic(ArcticCallback, BackendCallback):
    default_key = TRADES


class FundingArctic(ArcticCallback, BackendCallback):
    default_key = FUNDING


class TickerArctic(ArcticCallback, BackendCallback):
    default_key = TICKER


class OpenInterestArctic(ArcticCallback, BackendCallback):
    default_key = OPEN_INTEREST


class LiquidationsArctic(ArcticCallback, BackendCallback):
    default_key = LIQUIDATIONS


class CandlesArctic(ArcticCallback, BackendCallback):
    default_key = CANDLES


class OrderInfoArctic(ArcticCallback, BackendCallback):
    default_key = ORDER_INFO


class TransactionsArctic(ArcticCallback, BackendCallback):
    default_key = TRANSACTIONS


class BalancesArctic(ArcticCallback, BackendCallback):
    default_key = BALANCES


class FillsArctic(ArcticCallback, BackendCallback):
    default_key = FILLS
