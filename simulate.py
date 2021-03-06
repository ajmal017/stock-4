import alpaca_trade_api as tradeapi
import argparse
import datetime
import logging
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import os
import pandas as pd
import signal
import utils
from tabulate import tabulate


class TradingSimulate(utils.TradingBase):
    """Simulates trading transactions and outputs performances."""

    def __init__(self,
                 alpaca,
                 start_date=None,
                 end_date=None,
                 model=None,
                 data_files=None,
                 write_data=False):
        self.root_dir = os.path.dirname(os.path.realpath(__file__))
        self.output_dir = os.path.join(self.root_dir, utils.OUTPUTS_DIR,
                                       'simulate',
                                       datetime.datetime.now().strftime('%Y-%m-%d-%H-%M'))
        os.makedirs(self.output_dir, exist_ok=True)
        utils.logging_config(os.path.join(self.output_dir, 'result.txt'))
        self.write_data = write_data

        period = None
        if data_files:
            self.data_df = pd.concat([pd.read_csv(data_file) for data_file in data_files])
            year_diff = (datetime.datetime.today().date().year -
                         pd.to_datetime(self.data_df.iloc[0]['Date']).year + 1)
            period = '%dy' % (year_diff,)
        super(TradingSimulate, self).__init__(alpaca, period=period, start_date=start_date,
                                              end_date=end_date, model=model,
                                              load_history=not bool(data_files))
        self.data_files = data_files
        if self.data_files:
            self.start_date = start_date or self.data_df.iloc[0].Date
            self.end_date = end_date or self.data_df.iloc[-1].Date
            self.values = {'Total': (
                [self.get_prev_market_date(pd.to_datetime(self.start_date))],
                [1.0])}
        else:
            self.start_date = (start_date or
                               self.history_dates[utils.DAYS_IN_A_YEAR + 1].strftime('%F'))
            self.end_date = end_date or datetime.datetime.today().strftime('%F')
            self.start_point, self.end_point = 0, self.history_length - 1
            while (self.start_point < self.history_length and
                   pd.to_datetime(self.start_date) > self.history_dates[self.start_point]):
                self.start_point += 1
            while (self.end_point > 0 and
                   pd.to_datetime(self.end_date) < self.history_dates[self.end_point]):
                self.end_point -= 1
            if self.write_data:
                stats_cols = ['Symbol', 'Date'] + utils.ML_FEATURES + ['Gain']
                self.stats = pd.DataFrame(columns=stats_cols)
            self.values = {'Total': ([self.history_dates[self.start_point - 1]], [1.0])}
        self.win_trades, self.lose_trades = 0, 0
        signal.signal(signal.SIGINT, self.safe_exit)

    def safe_exit(self, signum, frame):
        logging.info('Safe exiting with signal %d...', signum)
        if self.write_data:
            self.save_data()
        else:
            self.print_summary()
        exit(1)

    def analyze_date(self, sell_date, cutoff):
        outputs = [utils.get_header(sell_date.date())]
        buy_symbols = self.get_buy_symbols(cutoff=cutoff, skip_prediction=self.write_data)
        if self.write_data and cutoff < self.history_length - 1:
            self.append_stats(buy_symbols, sell_date, cutoff)
            logging.info('\n'.join(outputs))
            return
        trading_list = self.get_trading_list(buy_symbols=buy_symbols)
        trading_table = []
        daily_gain = 0
        for symbol, proportion, weight, _ in trading_list:
            if proportion == 0:
                continue
            close = self.closes[symbol]
            today_change = close[cutoff] / close[cutoff - 1] - 1
            if cutoff == self.history_length - 1:
                trading_table.append([symbol, '%.2f%%' % (proportion * 100,),
                                      weight,
                                      '%.2f%%' % (today_change * 100,),
                                      close[cutoff]])
                continue
            gain = (close[cutoff + 1] - close[cutoff]) / close[cutoff]
            # > 100% gain might caused by stock split. Do not calculate.
            if gain >= 1:
                continue
            trading_table.append([symbol, '%.2f%%' % (proportion * 100,),
                                  weight,
                                  '%.2f%%' % (today_change * 100,),
                                  close[cutoff],
                                  close[cutoff + 1],
                                  '%+.2f%%' % (gain * 100,)])
            daily_gain += gain * proportion

        if trading_table:
            outputs.append(tabulate(trading_table, headers=[
                'Symbol', 'Proportion', 'Weight', 'Today Change',
                'Buy Price', 'Sell Price', 'Gain'], tablefmt='grid'))
        if cutoff < self.history_length - 1:
            self.add_profit(sell_date, daily_gain, outputs)
        else:
            logging.info('\n'.join(outputs))

    def analyze_rows(self, sell_date_str, rows):
        X, symbols, gains = [], [], {}
        for row in rows:
            x_value = [row[col] for col in utils.ML_FEATURES]
            X.append(x_value)
            symbols.append(row['Symbol'])
            gains[row.Symbol] = row['Gain']
        X = np.array(X)
        classifications = self.model.predict(X)
        buy_symbols = [(symbol, classification, None) for symbol, classification in zip(symbols, classifications)]
        trading_list = self.get_trading_list(buy_symbols=buy_symbols)
        trading_table = []
        daily_gain = 0
        for symbol, proportion, weight, side in trading_list:
            if proportion == 0:
                continue
            gain = gains[symbol] if side == 'long' else -gains[symbol]
            # > 100% gain might caused by stock split. Do not calculate.
            if gain >= 1:
                continue
            if gain > 0:
                self.win_trades += 1
            elif gain < 0:
                self.lose_trades += 1
            trading_table.append([symbol, '%.2f%%' % (proportion * 100,),
                                  weight,
                                  side,
                                  '%.2f%%' % (gain * 100,)])
            daily_gain += gain * proportion
        outputs = [utils.get_header(sell_date_str)]
        if trading_table:
            outputs.append(tabulate(
                trading_table,
                headers=['Symbol', 'Proportion', 'Weight', 'Side', 'Gain'],
                tablefmt='grid'))
        self.add_profit(pd.to_datetime(sell_date_str), daily_gain, outputs)

    def add_profit(self, sell_date, daily_gain, outputs):
        """Adds daily gain to values memory."""
        total_value = self.values['Total'][1][-1] * (1 + daily_gain)
        self.values['Total'][0].append(sell_date)
        self.values['Total'][1].append(total_value)
        quarter = '%d-Q%d' % (sell_date.year,
                              (sell_date.month - 1) // 3 + 1)
        year = '%d' % (sell_date.year,)
        for t in [quarter, year]:
            if t not in self.values:
                self.values[t] = ([self.get_prev_market_date(sell_date)],
                                  [1.0])
            self.values[t][0].append(sell_date)
            t_value = self.values[t][1][-1] * (1 + daily_gain)
            self.values[t][1].append(t_value)
        summary_table = [['Daily Gain', '%+.2f%%' % (daily_gain * 100),
                          'Quarterly Gain', '%+.2f%%' % ((self.values[quarter][1][-1] - 1) * 100,),
                          'Yearly Gain', '%+.2f%%' % ((self.values[year][1][-1] - 1) * 100,),
                          'Total Gain', '%+.2f%%' % ((total_value - 1) * 100,)],
                         ['Win Trades', self.win_trades, 'Lose Trades', self.lose_trades]]
        outputs.append(tabulate(summary_table, tablefmt='grid'))
        logging.info('\n'.join(outputs))

    def save_data(self):
        start_year = self.start_date[:4]
        end_year = self.start_date[:4]
        filename = ('data_%s.csv' % (start_year,) if start_year == end_year else
                    'data_%s_%s.csv' % (start_year, end_year))
        self.stats.to_csv(os.path.join(self.root_dir, utils.DATA_DIR, filename),
                          index=False)

    def print_summary(self):
        time_range = '%s ~ %s' % (self.start_date, self.end_date)
        summary_table = [['Time Range', time_range]]
        gain_texts = [(k + ' Gain', '%.2f%%' % ((v[1][-1] - 1) * 100,))
                      for k, v in self.values.items()]
        summary_table.extend(sorted(gain_texts))
        logging.info(utils.get_header('Summary') + '\n' + tabulate(summary_table, tablefmt='grid'))

    def plot_summary(self):
        pd.plotting.register_matplotlib_converters()
        plot_symbols = ['QQQ', 'SPY', 'TQQQ']
        color_map = {'QQQ': '#78d237', 'SPY': '#FF6358', 'TQQQ': '#aa46be'}
        for symbol in [utils.REFERENCE_SYMBOL] + plot_symbols:
            if symbol not in self.hists:
                try:
                    self.load_history(symbol, self.period)
                except Exception:
                    pass
        for k, v in self.values.items():
            dates, values = v
            if k == 'Total':
                formatter = mdates.DateFormatter('%Y-%m-%d')
            else:
                formatter = mdates.DateFormatter('%m-%d')
            plt.figure(figsize=(10, 4))
            plt.plot(dates, values,
                     label='My Portfolio (%+.2f%%)' % ((values[-1] - 1) * 100,),
                     color='#28b4c8')
            curve_max = 1
            for symbol in plot_symbols:
                if symbol in self.hists:
                    curve = [self.hists[symbol].get('Close')[dt] for dt in dates]
                    for i in range(len(dates) - 1, -1, -1):
                        curve[i] /= curve[0]
                    curve_max = max(curve_max, np.abs(curve[-1]))
                    plt.plot(dates, curve,
                             label='%s (%+.2f%%)' % (symbol, (curve[-1] - 1) * 100),
                             color=color_map[symbol])
            text_kwargs = {'family': 'monospace'}
            plt.xlabel('Date', **text_kwargs)
            plt.ylabel('Normalized Value', **text_kwargs)
            plt.title(k, **text_kwargs, y=1.15)
            plt.grid(linestyle='--', alpha=0.5)
            plt.legend(ncol=len(plot_symbols) + 1, bbox_to_anchor=(0, 1),
                       loc='lower left', prop=text_kwargs)
            ax = plt.gca()
            ax.spines['right'].set_color('none')
            ax.spines['top'].set_color('none')
            ax.xaxis.set_major_formatter(formatter)
            if np.abs(values[-1]) > 5 * curve_max:
                plt.yscale('log')
            plt.tight_layout()
            plt.savefig(os.path.join(self.output_dir, k + '.png'))
            plt.close()

    def run(self):
        """Starts simulation."""
        # Buy on cutoff day, sell on cutoff + 1 day
        if self.data_files:
            rows = []
            prev_date = ''
            for _, row in self.data_df.iterrows():
                current_date = row.Date
                if current_date < self.start_date or current_date > self.end_date:
                    continue
                if current_date != prev_date and prev_date:
                    self.analyze_rows(prev_date, rows)
                    rows = []
                rows.append(row)
                prev_date = current_date
            self.analyze_rows(prev_date, rows)
        else:
            for cutoff in range(self.start_point - 1, self.end_point):
                sell_date = self.history_dates[cutoff + 1]
                self.analyze_date(sell_date, cutoff)
            if pd.to_datetime(self.end_date) > self.history_dates[-1]:
                self.analyze_date(self.history_dates[-1] + pd.tseries.offsets.BDay(1),
                                  self.history_length - 1)

        if self.write_data:
            self.save_data()
        else:
            self.print_summary()
            self.plot_summary()

    def append_stats(self, buy_symbols, date, cutoff):
        for symbol, _, ml_feature in buy_symbols:
            close = self.closes[symbol]
            gain = (close[cutoff + 1] - close[cutoff]) / close[cutoff]
            # > 100% gain might caused by stock split. Do not calculate.
            if gain >= 1:
                continue
            stat_value = ml_feature
            stat_value['Symbol'] = symbol
            stat_value['Date'] = date
            stat_value['Gain'] = gain
            self.stats = self.stats.append(stat_value, ignore_index=True)

    def get_prev_market_date(self, date):
        p = 0
        while date > self.history_dates[p]:
            p += 1
        return self.history_dates[p - 1]


def main():
    parser = argparse.ArgumentParser(description='Stock trading simulation.')
    parser.add_argument('--start_date', default=None,
                        help='Start date of the simulation.')
    parser.add_argument('--end_date', default=None,
                        help='End date of the simulation.')
    parser.add_argument('--api_key', default=None, help='Alpaca API key.')
    parser.add_argument('--api_secret', default=None, help='Alpaca API secret.')
    parser.add_argument('--model', default=None, help='Keras model for weight prediction.')
    parser.add_argument('--data_files', default=None, nargs='*', help='Read datafile for simulation.')
    parser.add_argument("--write_data", help='Write data with ML features.',
                        action="store_true")
    args = parser.parse_args()

    alpaca = tradeapi.REST(args.api_key or os.environ['ALPACA_PAPER_API_KEY'],
                           args.api_secret or os.environ['ALPACA_PAPER_API_SECRET'],
                           utils.ALPACA_PAPER_API_BASE_URL, 'v2')
    trading = TradingSimulate(alpaca, args.start_date, args.end_date,
                              args.model, args.data_files,
                              args.write_data)
    trading.run()


if __name__ == '__main__':
    main()
