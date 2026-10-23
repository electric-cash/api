from blockchain.blockchain_analyzer import BlockchainAnalyzer
from db.db import get_db_uri, drop_db, execute_query, get_address, get_addresses, get_blockchain, get_highest_block_number_in_db, \
               get_utxos, insert_address, insert_block
from db.db_utils import create_address
from decimal import Decimal
from mongobackup import backup
from mongoengine import connect
from blockchain.rpc import get_block, get_block_count, get_block_hash
from db import query_cache
import logging, shutil, sys, time

# logging.basicConfig(level=logging.DEBUG, filename="daemon.log", format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%d-%m-%Y %H:%M:%S')
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(message)s',
    datefmt='%d-%m-%Y %H:%M:%S',
    handlers=[
        logging.StreamHandler()
    ]
)

analyzer = BlockchainAnalyzer()
BACKUP_FEATURE_HEIGHT = 0 # doesnt dump db before that chain height
BACKUP_INTERVAL = 1000 # ask for db dump every 1000th block
MAX_CHUNK_SIZE = 2000

period_block_map = {
    'hour' : 6,
    'day' : 144,
    'week' : 1008,
    'month' : 4320
}

def _crop_params(interval, lower_height, upper_height):
    if interval < 1: interval = 1
    elif interval > 4320: interval = 4320
    
    if lower_height < 1: lower_height = 1
    elif lower_height > analyzer.max_block_number(): lower_height = analyzer.max_block_number()
    
    if upper_height < lower_height: upper_height = lower_height
    elif upper_height > analyzer.max_block_number(): upper_height = analyzer.max_block_number()
    
    return interval, lower_height, upper_height

def period_to_block_range_and_interval(period):
    upper_height = analyzer.max_block_number()
    
    if period == 'week': return _crop_params(period_block_map['hour'], upper_height - period_block_map['week'], upper_height)
    if period == 'month' : return _crop_params(period_block_map['day'], upper_height - period_block_map['month'], upper_height)
    
    return _crop_params(period_block_map['week'], 1, upper_height)

        
def update_addresses(new_utxos, del_utxos):
    def _update_balance(db_utxo, addresses, new_utxo=True):
        addr = db_utxo.address
        amount = Decimal(db_utxo.value)
        if addr not in addresses:
            addresses[addr] = get_address(addr)
        
        if new_utxo:
            addresses[addr].balance += amount
        else:
            addresses[addr].balance -= amount
            
    addresses = {}
    for db_utxo in new_utxos.values():
        _update_balance(db_utxo, addresses)
        
        txid = db_utxo.id[:-1]
        addr = db_utxo.address
        if txid not in addresses[addr].txs:
            addresses[addr].txs.append(txid)
            
    for db_utxo in del_utxos.values():
        _update_balance(db_utxo, addresses, False)
            
    for address in addresses.values():
        insert_address(address)
    
def update_analyzer():
    analyzer.set_blockchain(get_blockchain())
    analyzer.set_addresses(get_addresses())
    analyzer.set_utxos(get_utxos())
        
def restore_db(reorg=False):
    query_cache.invalidate()
    raise RuntimeError('Automatic database restore is disabled; verified recovery required')

def backup_db():
    current_height = analyzer.max_block_number()
    if current_height < BACKUP_FEATURE_HEIGHT:
        return
    
    try:
        logging.info("Doing backup at height: {}".format(current_height))
        backup('""', '""', "/var/backups/mongodb/", mongo_backup_directory_path='/tmp/mongo_dump_backup', purge_local=30)
    except Exception as e:
        logging.warn(e)
            
def check_reorg(chunk_size):
    current_height = analyzer.max_block_number()
    if current_height < BACKUP_FEATURE_HEIGHT:
        return False
    
    reorg = not analyzer.check_chain(chunk_size)
    if reorg:
        query_cache.invalidate()
        raise RuntimeError('Stored blockchain is inconsistent; verified recovery required')
        
    return reorg


def _verify_stored_tip(node_height):
    tip = get_blockchain().only('hash', 'height').order_by('-height').first()
    if tip is not None and (tip.height > node_height or get_block_hash(tip.height) != tip.hash):
        query_cache.invalidate()
        raise RuntimeError('Stored blockchain tip is not canonical; verified recovery required')
    return tip
    
def update_blockchain():
    def _reexecute_queries():
        analyzer.piechart_data(100, 1, analyzer.max_block_number())
        periods = ['week', 'month', 'all']
        for period in periods:
            interval, lower_height, upper_height = period_to_block_range_and_interval(period)
            analyzer.mining_difficulty(interval, lower_height, upper_height)
            analyzer.transaction_volume(interval, lower_height, upper_height)
    
    query_cache.assert_no_pending_update()
    start_block_number = analyzer.max_block_number() + 1
    end_block_number = get_block_count() + 1
    tip = _verify_stored_tip(end_block_number - 1)
    if start_block_number == end_block_number:
        return

    generation = None
    previous_hash = tip.hash if tip is not None else None
    chunk_size = end_block_number - start_block_number
    iterations = chunk_size // MAX_CHUNK_SIZE + 1
    backup = False
    
    try:
        for i in range(iterations):
            logging.debug("Processing chunk: {}/{}".format(i + 1, iterations))
            new_utxos = {}
            del_utxos = {}
            new_start_block_number = start_block_number + i * MAX_CHUNK_SIZE
            new_end_block_number = min(new_start_block_number + MAX_CHUNK_SIZE, end_block_number)
            
            for idx in range(new_start_block_number, new_end_block_number):
                logging.debug("Processing block = {}".format(idx))
#                 backup = backup or idx % BACKUP_INTERVAL == 0
                
                block = get_block(idx)
                if (block.get('height') != idx or
                        (previous_hash is not None and block.get('previousblockhash') != previous_hash)):
                    query_cache.invalidate()
                    raise RuntimeError('Fetched block does not extend the canonical chain; verified recovery required')
                n_utxos = {}
                d_utxos = {}

                if generation is None:
                    generation = query_cache.begin_update()
                insert_block(block, n_utxos, d_utxos)
                previous_hash = block['hash']
                new_utxos = {**new_utxos, **n_utxos}
                del_utxos = {**del_utxos, **d_utxos}
                update_analyzer()
                
            if start_block_number > 0:
                check_reorg(min(chunk_size, MAX_CHUNK_SIZE))
            
            update_addresses(new_utxos, del_utxos)

#         if backup:
#             backup_db()
    except Exception as e:
        logging.error(e)
        raise
    
    _verify_stored_tip(get_block_count())
    query_cache.complete_update(generation)
    _reexecute_queries()

def init_cleanup():
    shutil.rmtree('/tmp/mongo_dump_backup', True)
    shutil.rmtree('/tmp/mongo_dump_restore', True)

def try_to_connect():
    try:
        connect(host=get_db_uri())
    except Exception as e:
        logging.error(e)
        raise
    
if __name__ == '__main__':
#     init_cleanup()
    try_to_connect()
#     restore_db()
    update_analyzer()
    while True:
        update_blockchain()
        time.sleep(5)
