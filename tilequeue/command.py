from contextlib import closing
from itertools import chain
from tilequeue.config import make_config_from_argparse
from tilequeue.format import lookup_format_by_extension
from tilequeue.metro_extract import city_bounds
from tilequeue.metro_extract import parse_metro_extract
from tilequeue.queue import make_sqs_queue
from tilequeue.render import RenderJobCreator
from tilequeue.store import make_s3_store
from tilequeue.store import make_tile_file_store
from tilequeue.tile import deserialize_coord
from tilequeue.tile import explode_with_parents
from tilequeue.tile import parse_expired_coord_string
from tilequeue.tile import seed_tiles
from tilequeue.tile import serialize_coord
from tilequeue.tile import tile_generator_for_multiple_bounds
from TileStache import parseConfigfile
from urllib2 import urlopen
import argparse
import logging
import logging.config
import os
import sys


def add_config_options(parser):
    parser.add_argument('--config')
    return parser


def add_aws_cred_options(parser):
    parser.add_argument('--aws_access_key_id')
    parser.add_argument('--aws_secret_access_key')
    return parser


def add_queue_options(parser):
    parser.add_argument('--queue-name',
                        help='Name of the queue, should already exist.',
                        )
    parser.add_argument('--queue-type',
                        default='sqs',
                        choices=('sqs', 'mem', 'file'),
                        help='Queue type, useful to change for testing.',
                        )
    parser.add_argument('--sqs-read-timeout',
                        type=int,
                        default=20,
                        help='Read timeout in seconds when reading '
                             'sqs messages.',
                        )
    return parser


def add_s3_options(parser):
    parser.add_argument('--s3-bucket',
                        help='Name of aws s3 bucket, should already exist.',
                        )
    parser.add_argument('--s3-reduced-redundancy',
                        action='store_true',
                        default=False,
                        help='Store tile data in s3 with reduced redundancy.',
                        )
    parser.add_argument('--s3-path',
                        default='',
                        help='Store tile data in s3 with this path prefix.',
                        )
    return parser


def add_tilestache_config_options(parser):
    parser.add_argument('--tilestache-config',
                        help='Path to Tilestache config.',
                        )
    return parser


def add_output_format_options(parser):
    parser.add_argument('--output-formats',
                        nargs='+',
                        choices=('json', 'vtm', 'topojson', 'mapbox'),
                        default=('json', 'vtm'),
                        help='Output formats to produce for each tile.',
                        )
    return parser


def add_logging_options(parser):
    parser.add_argument('--logconfig',
                        help='Path to python logging config file.',
                        )
    return parser


def make_queue(queue_type, queue_name, cfg):
    if queue_type == 'sqs':
        return make_sqs_queue(
            queue_name, cfg.aws_access_key_id, cfg.aws_secret_access_key)
    elif queue_type == 'mem':
        from tilequeue.queue import MemoryQueue
        return MemoryQueue()
    elif queue_type == 'file':
        # only support file queues for writing
        # useful for testing
        from tilequeue.queue import OutputFileQueue
        fp = open(queue_name, 'w')
        return OutputFileQueue(fp)
    else:
        raise ValueError('Unknown queue type: %s' % queue_type)


def tilequeue_parser_write(parser):
    parser = add_config_options(parser)
    parser = add_aws_cred_options(parser)
    parser = add_queue_options(parser)
    parser = add_logging_options(parser)
    parser.add_argument('--expired-tiles-file',
                        help='Path to file containing list of expired tiles. '
                             'Should be one per line, <zoom>/<column>/<row>',
                        )
    parser.set_defaults(func=tilequeue_write)
    return parser


def tilequeue_parser_read(parser):
    parser = add_config_options(parser)
    parser = add_aws_cred_options(parser)
    parser = add_queue_options(parser)
    parser = add_logging_options(parser)
    parser.set_defaults(func=tilequeue_read)
    return parser


def tilequeue_read(cfg):
    assert cfg.queue_name, 'Missing queue name'
    logger = make_logger(cfg, 'read')
    queue = make_queue(cfg.queue_type, cfg.queue_name, cfg)
    msgs = queue.read(max_to_read=1, timeout_seconds=cfg.read_timeout)
    if not msgs:
        logger.info('No messages found on queue: %s' % cfg.queue_name)
    for msg in msgs:
        coord = msg.coord
        logger.info('Received tile: %s' % serialize_coord(coord))


def tilequeue_parser_process(parser):
    parser = add_config_options(parser)
    parser = add_aws_cred_options(parser)
    parser = add_queue_options(parser)
    parser = add_s3_options(parser)
    parser = add_tilestache_config_options(parser)
    parser = add_output_format_options(parser)
    parser = add_logging_options(parser)
    parser.add_argument('--daemon',
                        action='store_true',
                        default=False,
                        help='Enable daemon mode, which will continue to poll '
                             'for messages',
                        )
    parser.set_defaults(func=tilequeue_process)
    return parser


def tilequeue_parser_seed(parser):
    parser = add_config_options(parser)
    parser = add_aws_cred_options(parser)
    parser = add_queue_options(parser)
    parser = add_logging_options(parser)
    parser.add_argument('--zoom-start',
                        type=int,
                        default=0,
                        choices=xrange(21),
                        help='Zoom level to start seeding tiles with.',
                        )
    parser.add_argument('--zoom-until',
                        type=int,
                        default=0,
                        choices=xrange(21),
                        help='Zoom level to seed tiles until, inclusive.',
                        )
    parser.add_argument('--metro-extract-url',
                        help='Url to metro extracts json (or file://).',
                        )
    parser.add_argument('--filter-metro-zoom',
                        type=int,
                        default=0,
                        choices=xrange(21),
                        help='Zoom level to start filtering for '
                             'metro extracts.',
                        )
    parser.add_argument('--unique-tiles',
                        default=False,
                        action='store_true',
                        help='Only generate unique tiles. The bounding boxes '
                        'in metro extracts overlap, which will generate '
                        'duplicate tiles for the overlaps. This flag ensures '
                        'that the tiles will be unique.',
                        )
    parser.set_defaults(func=tilequeue_seed)
    return parser


def tilequeue_parser_generate_tile(parser):
    parser = add_config_options(parser)
    parser = add_aws_cred_options(parser)
    parser = add_s3_options(parser)
    parser = add_tilestache_config_options(parser)
    parser = add_output_format_options(parser)
    parser = add_logging_options(parser)
    parser.add_argument('--tile',
                        help='Tile coordinate used to generate a tile. Must '
                        'be of the form: <zoom>/<column>/<row>',
                        )
    parser.set_defaults(func=tilequeue_generate_tile)
    return parser


def assert_aws_config(cfg):
    if (cfg.aws_access_key_id is not None or
            cfg.aws_secret_access_key is not None):
        # assert that if either is specified, both are specified
        assert (cfg.aws_access_key_id is not None and
                cfg.aws_secret_access_key is not None), \
            'Must specify both aws key and secret'


def tilequeue_write(cfg):

    assert_aws_config(cfg)

    assert os.path.exists(cfg.expired_tiles_file), \
        'Invalid expired tiles path'

    assert cfg.queue_name, 'Missing queue name'
    queue = make_queue(cfg.queue_type, cfg.queue_name, cfg)

    expired_tiles = []
    with open(cfg.expired_tiles_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            coord = parse_expired_coord_string(line)
            if coord is None:
                print 'Could not parse coordinate from line: ' % line
                continue
            expired_tiles.append(coord)

    logger = make_logger(cfg, 'write')

    logger.info('Number of expired tiles: %d' % len(expired_tiles))

    exploded_coords = explode_with_parents(expired_tiles)
    logger.info('Number of total expired tiles with all parents: %d' %
                len(exploded_coords))

    logger.info('Queuing ... ')

    # exploded_coords is a set, but enqueue_batch expects a list for slicing
    exploded_coords = list(exploded_coords)
    queue.enqueue_batch(exploded_coords)

    logger.info('Queuing ... Done')
    logger.info('Queued %d tiles' % len(exploded_coords))


def lookup_formats(format_extensions):
    formats = []
    for extension in format_extensions:
        format = lookup_format_by_extension(extension)
        assert format is not None, 'Unknown extension: %s' % extension
        formats.append(format)
    return formats


def process_jobs_for_coord(coord, job_creator, store):
    jobs = job_creator.create(coord)
    for job in jobs:
        with closing(store.output_fp(coord, job.format)) as store_fp:
            job(store_fp)


def make_logger(cfg, logger_name):
    if hasattr(cfg, 'logconfig'):
        logging.config.fileConfig(cfg.logconfig)
    logger = logging.getLogger(logger_name)
    return logger


def tilequeue_process(cfg):
    assert_aws_config(cfg)

    logger = make_logger(cfg, 'process')

    assert os.path.exists(cfg.tilestache_config), \
        'Invalid tilestache config path'

    formats = lookup_formats(cfg.output_formats)

    queue = make_queue(cfg.queue_type, cfg.queue_name, cfg)

    tilestache_config = parseConfigfile(cfg.tilestache_config)
    job_creator = RenderJobCreator(tilestache_config, formats)

    store = make_s3_store(
        cfg.s3_bucket, cfg.aws_access_key_id, cfg.aws_secret_access_key,
        path=cfg.s3_path, reduced_redundancy=cfg.s3_reduced_redundancy)

    if cfg.daemon:
        logger.info('Starting tilequeue processing in daemon mode')
    else:
        logger.info('Starting tilequeue processing, not in daemon mode')

    n_msgs = 0
    while True:
        msgs = queue.read(max_to_read=1, timeout_seconds=cfg.read_timeout)
        if not msgs and not cfg.daemon:
            break
        for msg in msgs:
            coord = msg.coord
            coord_str = serialize_coord(coord)
            logger.info('processing %s ...' % coord_str)
            process_jobs_for_coord(coord, job_creator, store)
            queue.job_done(msg.message_handle)
            logger.info('processing %s ... done' % coord_str)
            n_msgs += 1

    logger.info('processed %d messages' % n_msgs)


def tilequeue_seed_process(tile_generator, queue):
    # enqueue in batches of 10
    batch = []
    n_tiles = 0
    for tile in tile_generator:
        batch.append(tile)
        n_tiles += 1
        if len(batch) >= 10:
            queue.enqueue_batch(batch)
            batch = []
    if batch:
        queue.enqueue_batch(batch)
    return n_tiles


def uniquify_generator(generator):
    s = set(generator)
    for tile in s:
        yield tile


def tilequeue_seed(cfg):
    if cfg.queue_type == 'sqs':
        assert_aws_config(cfg)

    if cfg.metro_extract_url:
        assert cfg.filter_metro_zoom is not None, \
            '--filter-metro-zoom is required when specifying a ' \
            'metro extract url'
        assert cfg.filter_metro_zoom <= cfg.zoom_until
        with closing(urlopen(cfg.metro_extract_url)) as fp:
            # will raise a MetroExtractParseError on failure
            metro_extracts = parse_metro_extract(fp)
        multiple_bounds = city_bounds(metro_extracts)
        filtered_tiles = tile_generator_for_multiple_bounds(
            multiple_bounds, cfg.filter_metro_zoom, cfg.zoom_until)
        # unique tiles will force storing a set in memory
        if cfg.unique_tiles:
            filtered_tiles = uniquify_generator(filtered_tiles)
        unfiltered_end_zoom = cfg.filter_metro_zoom - 1
    else:
        assert not cfg.filter_metro_zoom, \
            '--metro-extract-url is required when specifying ' \
            '--filter-metro-zoom'
        filtered_tiles = ()
        unfiltered_end_zoom = cfg.zoom_until

    assert cfg.zoom_start <= unfiltered_end_zoom

    unfiltered_tiles = seed_tiles(cfg.zoom_start, unfiltered_end_zoom)

    tile_generator = chain(unfiltered_tiles, filtered_tiles)

    queue = make_queue(cfg.queue_type, cfg.queue_name, cfg)

    logger = make_logger(cfg, 'seed')

    logger.info('Beginning to enqueue seed tiles')

    n_tiles = tilequeue_seed_process(tile_generator, queue)

    logger.info('Queued %d tiles' % n_tiles)


def tilequeue_generate_tile(cfg):
    assert cfg.tile, 'Missing tile coordinate'
    tile_str = cfg.tile

    coord = deserialize_coord(tile_str)
    assert coord is not None, 'Could not parse tile from %s' % tile_str

    tilestache_config = parseConfigfile(cfg.tilestache_config)
    formats = lookup_formats(cfg.output_formats)
    job_creator = RenderJobCreator(tilestache_config, formats)

    if cfg.s3_bucket:
        store = make_s3_store(
            cfg.s3_bucket, cfg.aws_access_key_id, cfg.aws_secret_access_key,
            path=cfg.s3_path, reduced_redundancy=cfg.s3_reduced_redundancy)
    else:
        store = make_tile_file_store(sys.stdout)

    process_jobs_for_coord(coord, job_creator, store)

    sys.stdout = open("/dev/stdout", "w")
    logger = make_logger(cfg, 'generate_tile')
    logger.info('Generated tile for: %s' % tile_str)


class TileArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error: %s\n' % message)
        self.print_help()
        sys.exit(2)


def tilequeue_main(argv_args=None):
    if argv_args is None:
        argv_args = sys.argv[1:]

    parser = TileArgumentParser()
    subparsers = parser.add_subparsers()

    parser_config = (
        ('write', tilequeue_parser_write),
        ('process', tilequeue_parser_process),
        ('read', tilequeue_parser_read),
        ('seed', tilequeue_parser_seed),
        ('generate-tile', tilequeue_parser_generate_tile),
    )
    for parser_name, parser_func in parser_config:
        subparser = subparsers.add_parser(parser_name)
        parser_func(subparser)

    args = parser.parse_args(argv_args)
    cfg = make_config_from_argparse(args)
    args.func(cfg)