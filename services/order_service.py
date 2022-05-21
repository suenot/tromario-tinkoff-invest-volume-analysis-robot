import csv
import logging
import threading
from itertools import groupby
from os.path import exists

import pandas as pd
from tinkoff.invest import Client, OrderType, OrderDirection

from services.telegram_service import TelegramService
from settings import NOTIFICATION, SANDBOX_ACCOUNT_ID, INSTRUMENTS, TOKEN
from utils.order_util import is_order_already_open
from utils.utils import Utils

logger = logging.getLogger(__name__)

orders_file_path = './data/orders.csv'


def write_file(order):
    try:
        with open(orders_file_path, 'a', newline='') as file:
            writer = csv.writer(file)
            if file.tell() == 0:
                writer.writerow(order.keys())
            writer.writerow(order.values())
    except Exception as ex:
        logger.error(ex)


def load_orders():
    orders = []

    if not exists(orders_file_path):
        return orders

    try:
        with open(orders_file_path, newline='') as file:
            reader = csv.DictReader(file)
            # header = next(reader)
            for order in reader:
                order['open'] = float(order['open'])
                order['stop'] = float(order['stop'])
                order['take'] = float(order['take'])
                order['time'] = pd.to_datetime(order['time'])
                order['direction'] = int(order['direction'])
                orders.append(order)
    except Exception as ex:
        logger.error(ex)

    return orders


# в отдельном потоке, чтобы не замедлял процесс обработки
class OrderService(threading.Thread):
    def __init__(self, is_notification=False, is_open_orders=False):
        super().__init__()

        self.telegram_service = TelegramService(NOTIFICATION['bot_token'], NOTIFICATION['chat_id'])

        self.is_notification = is_notification
        self.is_open_orders = is_open_orders
        self.orders = load_orders()

    def create_order(self, order):
        try:
            if order is None:
                return

            if is_order_already_open(self.orders, order):
                logger.info(f'сделка уже открыта: {order}')
                return

            instrument = next(item for item in INSTRUMENTS if item["name"] == order['instrument'])

            if self.is_open_orders:
                with Client(TOKEN) as client:
                    # todo может возникнуть ситуация, когда будет создано 100 позиций с 1 лотом в каждой
                    #  сервер не позволит выполнить моментально 100 запросов
                    new_order = client.sandbox.post_sandbox_order(
                        account_id=SANDBOX_ACCOUNT_ID,
                        figi=instrument['future'],
                        quantity=1,
                        direction=order['direction'],
                        order_type=OrderType.ORDER_TYPE_MARKET,
                        order_id=order['id'])
                    logger.info(new_order)
                    order['order_id'] = new_order.order_id

            self.orders.append(order)
            write_file(order)

            logger.info(f"✅ ТВ {order['instrument']}: цена {order['open']}, тейк {order['take']}, стоп {order['stop']}")
            if self.is_notification:
                self.telegram_service.post(
                    f"✅ ТВ {order['instrument']}: цена {order['open']}, тейк {order['take']}, стоп {order['stop']}")
        except Exception as ex:
            logger.error(ex)

    def close_order(self, order):
        if self.is_notification:
            self.telegram_service.post(
                f"закрыта позиция на {order['instrument']}: результат {order['result']}")

    def processed_orders(self, instrument, current_price, time):
        for order in self.orders:
            if order['status'] == 'active':
                if not Utils.is_open_orders(time):
                    # закрытие сделок по причине приближении закрытии биржи
                    order['status'] = 'close'
                    order['close'] = current_price
                    if order['direction'] == OrderDirection.ORDER_DIRECTION_BUY.value:
                        order['result'] = order['close'] - order['open']
                        order['is_win'] = order['result'] > 0
                    else:
                        order['result'] = order['open'] - order['close']
                        order['is_win'] = order['result'] > 0
                    logger.info(f'закрытие открытой заявки [time={order["time"]}], результат: {order["result"]}')
                    self.close_order(order)
                    continue

                if order['instrument'] != instrument:
                    continue

                if order['direction'] == OrderDirection.ORDER_DIRECTION_BUY.value:
                    if current_price < order['stop']:
                        # закрываю активные buy-заявки по стопу, если цена ниже стоп-лосса
                        order['status'] = 'close'
                        order['close'] = current_price
                        order['is_win'] = False
                        order['result'] = order['close'] - order['open']
                        logger.info(
                            f'закрыта заявка по стоп-лоссу с результатом {order["result"]}; открыта в {order["time"]}, текущее время {time}')
                        self.close_order(order)
                    elif current_price > order['take']:
                        # закрываю активные buy-заявки по тейку, если цена выше тейк-профита
                        order['status'] = 'close'
                        order['close'] = current_price
                        order['is_win'] = True
                        order['result'] = order['close'] - order['open']
                        logger.info(
                            f'закрыта заявка по тейк-профиту с результатом {order["result"]}; открыта в {order["time"]}, текущее время {time}')
                        self.close_order(order)
                else:
                    if current_price > order['stop']:
                        # закрываю активные sell-заявки по стопу, если цена выше стоп-лосса
                        order['status'] = 'close'
                        order['close'] = current_price
                        order['is_win'] = False
                        order['result'] = order['open'] - order['close']
                        logger.info(
                            f'закрыта заявка по стоп-лоссу с результатом {order["result"]}; открыта в {order["time"]}, текущее время {time}')
                        self.close_order(order)
                    elif current_price < order['take']:
                        # закрываю активные sell-заявки по тейку, если цена ниже тейк-профита
                        order['status'] = 'close'
                        order['close'] = current_price
                        order['is_win'] = True
                        order['result'] = order['open'] - order['close']
                        logger.info(
                            f'закрыта заявка по тейк-профиту с результатом {order["result"]}; открыта в {order["time"]}, текущее время {time}')
                        self.close_order(order)

    def write_statistics(self):
        # if not self.df.empty:
        #     # по завершению анализа перестраиваю показания, т.к. закрытие торгов не совпадает целому часу
        #     # например 15:59:59.230333+00:00
        #     self.clusters = Utils.ticks_to_cluster(self.df, period=CURRENT_TIMEFRAME)
        #     valid_entry_points, invalid_entry_points = Utils.processed_volume_levels_to_times(
        #         self.processed_volume_levels)
        #     if IS_SHOW_CHART:
        #         self.finplot_graph.render(self.df,
        #                                   valid_entry_points=valid_entry_points,
        #                                   invalid_entry_points=invalid_entry_points,
        #                                   clusters=self.clusters)

        groups = groupby(self.orders, lambda order: order['instrument'])
        for instrument, group in groups:
            file_path = f'./logs/statistics-{instrument}.log'
            orders = list(group)
            with open(file_path, 'a', encoding='utf-8') as file:
                take_orders = list(filter(lambda x: x['is_win'], orders))
                earned_points = sum(order['result'] for order in take_orders)
                loss_orders = list(filter(lambda x: not x['is_win'], orders))
                lost_points = sum(order['result'] for order in loss_orders)
                total = earned_points + lost_points

                logger.info(f'инструмент: {instrument}')
                logger.info(f'количество сделок: {len(orders)}')
                logger.info(f'успешных сделок: {len(take_orders)}')
                logger.info(f'заработано пунктов: {earned_points}')
                logger.info(f'отрицательных сделок: {len(loss_orders)}')
                logger.info(f'потеряно пунктов: {lost_points}')
                logger.info(f'итого пунктов: {total}')
                logger.info('-------------------------------------')

                file.write(f'количество сделок: {len(orders)}\n')
                file.write(f'успешных сделок: {len(take_orders)}\n')
                file.write(f'заработано пунктов: {earned_points}\n')
                file.write(f'отрицательных сделок: {len(loss_orders)}\n')
                file.write(f'потеряно пунктов: {lost_points}\n\n')
                file.write(f'итого пунктов: {total}\n')
                file.write('-------------------------------------\n')
