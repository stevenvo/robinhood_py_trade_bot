import requests
import urllib2
import pandas as pd
from Robinhood import Robinhood
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.techindicators import TechIndicators
from alpha_vantage.sectorperformance import SectorPerformances
from time import sleep

import json
import pushover
import schedule
import time
import datetime
import numpy as np
from requests.exceptions import ConnectionError
import os
import logging

logging.basicConfig(format='%(asctime)s: %(message)s', datefmt='%Y/%m/%d %I:%M:%S %p', level=logging.INFO)

## Configure these values in your OS environment variables
## Windows: http://www.dowdandassociates.com/blog/content/howto-set-an-environment-variable-in-windows-command-line-and-registry/
## Mac: http://osxdaily.com/2015/07/28/set-enviornment-variables-mac-os-x/
## Linux: https://www.cyberciti.biz/faq/set-environment-variable-linux/

ROBINHOOD_USERNAME = os.environ['ROBINHOOD_USERNAME']
ROBINHOOD_PASSWORD = os.environ['ROBINHOOD_PASSWORD']
ALPHAVANTAGE_API_TOKEN = os.environ['ALPHAVANTAGE_API_TOKEN'] # Get your free key at https://www.alphavantage.co/

# Signup account with Pushover and install on your phone so you can receive notifications
PUSHOVER_TOKEN = os.environ['PUSHOVER_TOKEN']
PUSHOVER_USERKEY = os.environ['PUSHOVER_USERKEY']

PO_TITLE = "Robinhood"
REFRESH_SECURITIES_INTERVAL = 4 #minutes
INTRADAY_SIGNAL_INTERVAL = 15 #minutes
MAX_API_RETRIES = 3

slow_interval = 'daily'
fast_interval = '15min'
ema_fast_period = 12
ema_slow_period = 26
series_type = 'close'


po_client = pushover.Client(PUSHOVER_USERKEY, api_token=PUSHOVER_TOKEN)

r = Robinhood()
logged_in = r.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD)

ts = TimeSeries(key=ALPHAVANTAGE_API_TOKEN, output_format='pandas')
ti = TechIndicators(key=ALPHAVANTAGE_API_TOKEN, output_format='pandas')
sp = SectorPerformances(key=ALPHAVANTAGE_API_TOKEN, output_format='pandas')

data = {}
msg_template = {
  "MACD_OWN_BUY": "", #Owned stocks to HOLD: {}
  "MACD_OWN_SELL": "Owned stocks to SELL: {}",
  "MACD_WATCH_BUY": "Watched stocks to BUY: {}",
  "MACD_WATCH_SELL": "", #Watched stocks to stay away from: {}
  "PRICE_CROSS_EMA_OWN_EXIT_LONG": "Owned stocks exit LONG - consider SELL: {}",
  "PRICE_CROSS_EMA_OWN_EXIT_SHORT": "", #Owned stocks exit SHORT - to HOLD: {}
  "PRICE_CROSS_EMA_WATCH_EXIT_LONG": "", #Watched stocks to stay away from: {}
  "PRICE_CROSS_EMA_WATCH_EXIT_SHORT": "Watched stocks exit SHORT - consider BUY: {}",
}


# list dedup function
def f7(seq):
  seen = set()
  seen_add = seen.add
  return [x for x in seq if not (x in seen or seen_add(x))]

def lst_to_str(lst):
  return ', '.join(lst).encode('ascii', 'ignore')

def is_market_open():
  # return True # for debugging
  now = datetime.datetime.now()
  return datetime.time(13,30) <= now.time() <= datetime.time(20,00)

def combine_security_lists(own_list, watch_list, exclude_list):
  l = []
  global owned_securities, watched_securities
  owned_securities = []
  watched_securities = []
  for security in own_list:
      req = requests.get(security['instrument'])
      data = req.json()
      owned_securities.append(data['symbol'])
      if data['symbol'] not in l and data['symbol'] not in exclude_list:
        l.append(data['symbol'])

  for security in watch_list:
    req = requests.get(security['instrument'])
    data = req.json()
    watched_securities.append(data['symbol'])
    if data['symbol'] not in l and data['symbol'] not in exclude_list:
      l.append(data['symbol'])
  return l


#Global vars
unsupported_securities = ['ROKU'] #securities marked not supported by Alpha Vantage
owned_securities = [] #securities you currently own on Robinhood
watched_securities = [] #securities you currently watch on Robinhood
combined_securities = [] #combination of owned and watched lists


# Should execute these to refresh your holding securities
# on Robinhood every 2-5 minutes
def refresh_security_list_from_robinhood():
  logging.info("refresh_security_list_from_robinhood()")
  if is_market_open():

    global owned_securities, watched_securities, combined_securities
    owned_securities = []
    watched_securities = []
    combined_securities = []

    retry_count = 1
    while (retry_count <= MAX_API_RETRIES):
        try:
            combined_securities = combine_security_lists(
              r.securities_owned()['results'],
              r.securities_watched()['results'],
              unsupported_securities )
            break
        except urllib2.HTTPError: #Alpha Vantage API has high failure rate - retry 3 times
            logging.error("HTTPError exception calling Robinhood - Retry: {}/{}".format(symbol, retry_count, MAX_API_RETRIES))
            retry_count = retry_count + 1
            sleep(0.5)

# DAILY long interval evaluation of positions and provide suggestion on BUY / SELL signal on securities you own/watch
# Suggest to run 3 times: (1) before go to bed, (2) after market opens 30 minutes, (3) before market closes 30 minutes
# If MACD crosses over 0.0 - Signal BUY or SELL
def evaluate_daily_positions():
  logging.info("evaluate_daily_positions()")
  global owned_securities, watched_securities, combined_securities
  own_buy = []
  own_sell = []
  watch_buy = []
  watch_sell = []
  for symbol in combined_securities:
    logging.info("processing {}".format(symbol))
    retry_count = 1
    while (retry_count <= MAX_API_RETRIES):
      try:
        macd = ti.get_macd(symbol=symbol, interval=slow_interval, series_type=series_type)
        [macd_m2, macd_m1] = macd[0].tail(2)['MACD']
        if macd_m2 * macd_m1 <= 0: # change direction
            if macd_m2 <= 0: # buy signal
              if symbol in owned_securities:
                own_buy.append(symbol)
              else:
                watch_buy.append(symbol)
            else: # sell signal
              if symbol in owned_securities:
                own_sell.append(symbol)
              else:
                watch_sell.append(symbol)
        # else:
        #     print symbol
        break
      except KeyError as e:
        logging.error("[{}] is not supported by Alpha Vantage.".format(symbol))
        break
      except urllib2.HTTPError: #Alpha Vantage API has high failure rate - retry 3 times
        logging.error( "[{}] - Exception HTTPError - Retry: {}/{}".format(symbol, retry_count, MAX_API_RETRIES))
        retry_count = retry_count + 1
        sleep(0.5)
    sleep(0.5) # Time in seconds.


  if own_sell:
    logging.info( msg_template['MACD_OWN_SELL'].format(lst_to_str(own_sell)))
    po_client.send_message(msg_template['MACD_OWN_SELL'].format(lst_to_str(own_sell)), title=PO_TITLE)

  if watch_buy:
    logging.info( msg_template['MACD_WATCH_BUY'].format(lst_to_str(watch_buy)))
    po_client.send_message(msg_template['MACD_WATCH_BUY'].format(lst_to_str(watch_buy)), title=PO_TITLE)


# INTRADAY short interval evaluation of pisitions and provide suggestion on BUY / SELL signal on securities you own/watch
# If in LONG position and last price drops BELOW slow ema -> Exit LONG (can sell)
# IF in SHORT position and last price spikes ABOVE fast ema -> Exit SHORT (can buy)
# Run every x minutes - can use period defined in fast_interval - don't run this too frequent
def evaluate_intraday_positions():
  logging.info( "evaluate_intraday_positions()")
  if is_market_open():
    own_buy = []
    own_sell = []
    watch_buy = []
    watch_sell = []
    global owned_securities, watched_securities, combined_securities
    for symbol in combined_securities:
      logging.info( "processing {}".format(symbol))
      retry_count = 1
      while (retry_count <= MAX_API_RETRIES):
        try:
          fast_ema = ti.get_ema(symbol=symbol, interval=slow_interval, time_period=ema_fast_period, series_type=series_type)
          slow_ema = ti.get_ema(symbol=symbol, interval=slow_interval, time_period=ema_slow_period, series_type=series_type)
          price = ts.get_intraday(symbol=symbol, interval=fast_interval, outputsize='compact')
          last_fast_ema = fast_ema[0].tail(1)['EMA'][0]
          last_slow_ema = slow_ema[0].tail(1)['EMA'][0]
          last_price = price[0].tail(1)['close'][0]
          if last_fast_ema > last_slow_ema: #LONG
              if last_price < last_slow_ema:
                  if symbol in owned_securities:
                    own_sell.append(symbol)
                  else:
                    watch_sell.append(symbol)
          elif last_fast_ema < last_slow_ema: #SHORT
              if last_price > last_fast_ema:
                  if symbol in owned_securities:
                    own_buy.append(symbol)
                  else:
                    watch_buy.append(symbol)
          else: # EMA crossing!!!
            msg = "[{}] EMAs crossing, must BUY/SELL now!".format(symbol)
            logging.info( msg)
            po_client.send_message(msg, title=PO_TITLE)

            # TODO: process crossing better
            # if macd_m1 > macd_m2:
            #     print "[{}] EMAs crossing, must BUY now!".format(symbol)
            # else:
            #     print "[{}] EMAs crossing, must SELL now!".format(symbol)
          break
        except KeyError as e:
          logging.error( "[{}] is not supported by Alpha Vantage.".format(symbol))
          break
        except urllib2.HTTPError: #Alpha Vantage API has high failure rate - retry 3 times
          logging.error( "[{}] - Exception HTTPError - Retry: {}/{}".format(symbol, retry_count, MAX_API_RETRIES))
          retry_count = retry_count + 1
          sleep(0.5)
      sleep(0.5) # Time in seconds.


    if own_sell:
      logging.info( msg_template['PRICE_CROSS_EMA_OWN_EXIT_LONG'].format(lst_to_str(own_sell)))
      po_client.send_message(msg_template['PRICE_CROSS_EMA_OWN_EXIT_LONG'].format(lst_to_str(own_sell)), title=PO_TITLE)

    if watch_buy:
      logging.info( msg_template['PRICE_CROSS_EMA_WATCH_EXIT_SHORT'].format(lst_to_str(watch_buy)))
      po_client.send_message(msg_template['PRICE_CROSS_EMA_WATCH_EXIT_SHORT'].format(lst_to_str(watch_buy)), title=PO_TITLE)


logging.info( "Service start!")

refresh_security_list_from_robinhood()
evaluate_daily_positions()
evaluate_intraday_positions()

# Refresh security list from Robinhood every X minutes
schedule.every(REFRESH_SECURITIES_INTERVAL).minutes.do(refresh_security_list_from_robinhood)


# DAILY long interval evaluation of positions and provide suggestion on BUY / SELL signal on securities you own/watch
# Suggest to run 3 times: (1) before go to bed, (2) after market opens 30 minutes, (3) before market closes 30 minutes
schedule.every().day.at("06:00").do(evaluate_daily_positions) #6AM UTC ~ 11PM PST - before go to bed
schedule.every().day.at("14:00").do(evaluate_daily_positions) #2PM UTC ~ 10AM EST - 30 after market open
schedule.every().day.at("19:30").do(evaluate_daily_positions) #7.30PM UTC ~ 3.30PM EST - 30 before market close

# INTRADAY short interval evaluation of pisitions and provide suggestion on BUY / SELL signal on securities you own/watch
schedule.every(INTRADAY_SIGNAL_INTERVAL).minutes.do(evaluate_intraday_positions)

while True:
  schedule.run_pending()
  time.sleep(1)

