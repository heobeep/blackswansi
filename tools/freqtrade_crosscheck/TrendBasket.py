from freqtrade.strategy import IStrategy
from pandas import DataFrame


class TrendBasket(IStrategy):
    """Reference implementation of the bot's baseline idea, used to cross-check my backtester:
    hold a coin only while its daily close is above its EMA(50). Equal slots across the basket."""
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.99
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 200  # recursive-analysis: EMA needs >=199 candles to match steady state
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = dataframe["close"].ewm(span=50, adjust=False, min_periods=50).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["ema50"]), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] < dataframe["ema50"]), "exit_long"] = 1
        return dataframe
