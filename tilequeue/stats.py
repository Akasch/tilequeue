class TileProcessingStatsHandler(object):

    def __init__(self, stats):
        self.stats = stats

    def __call__(self, coord_proc_data):
        with self.stats.pipeline() as pipe:
            pipe.timing('process.fetch',
                        coord_proc_data.timing['fetch_seconds'])
            pipe.timing('process.process',
                        coord_proc_data.timing['process_seconds'])
            pipe.timing('process.upload', coord_proc_data.timing['s3_seconds'])
            pipe.timing('process.ack', coord_proc_data.timing['ack_seconds'])
            pipe.timing('process.time-in-queue',
                        coord_proc_data.timing['queue'])

            for layer_name, features_size in coord_proc_data.size.items():
                metric_name = 'process.size.%s' % layer_name
                pipe.gauge(metric_name, features_size)

            pipe.incr('process.storage.stored',
                      coord_proc_data.store_info['stored'])
            pipe.incr('process.storage.skipped',
                      coord_proc_data.store_info['not_stored'])


class RawrTileEnqueueStatsHandler(object):

    def __init__(self, stats):
        self.stats = stats

    def __call__(self, n_coords, n_payloads, n_msgs_sent):
        with self.stats.pipeline() as pipe:
            pipe.gauge('rawr.enqueue.coords', n_coords)
            pipe.gauge('rawr.enqueue.grouped', n_payloads)
            pipe.gauge('rawr.enqueue.calls', n_msgs_sent)
