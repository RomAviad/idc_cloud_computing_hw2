import datetime
import json
import requests
import pytz

from uhashring import HashRing
from redis import StrictRedis
from typing import Any, Dict, List, Optional, Union


class CacheRingManager(object):
    def __init__(
        self,
        ip,
        port,
        redis_client: StrictRedis,
        nodes_list_key: str,
        heartbeat_timeout: int,
        s3_bucket: str,
        s3_client,
    ):
        self.ip = ip
        self.port = port
        self.redis = redis_client
        self.nodes_list_key = nodes_list_key
        self.heartbeat_timeout = datetime.timedelta(seconds=heartbeat_timeout)
        self.cache_dict = {}
        self.bucket = s3_bucket
        self.s3_client = s3_client
        self.nodes_count = 1

        self.refresh_required = False
        self.set_heartbeat()
        self.refresh_cache()
        self.send_refresh_to_all_nodes()

    def now(self):
        return datetime.datetime.utcnow()

    def get_live_nodes(self):
        nodes_list = self.redis.keys(pattern="node_*")

        # "better safe than sorry", the python-redis results version
        nodes_list = [
            node.decode() if isinstance(node, bytes) else node for node in nodes_list
        ]

        result = []
        now = datetime.datetime.utcnow()
        for node_ip in nodes_list:
            last_heartbeat = float(self.redis.get(node_ip))
            last_heartbeat = datetime.datetime.fromtimestamp(last_heartbeat)
            if last_heartbeat and last_heartbeat > (now - self.heartbeat_timeout):
                result.append(node_ip.replace("node_", ""))

        if len(result) != self.nodes_count:
            self.refresh_required = True
        self.nodes_count = len(result)
        return result

    def set_heartbeat(self):
        now = self.now().timestamp()
        self.redis.set(
            name=f"node_{self.ip}", value=now, ex=self.heartbeat_timeout.seconds * 5
        )

        if self.refresh_required:
            self.refresh_cache()

        else:
            # clear expired entries from memory
            self.cache_dict = {
                key: value for key, value in self.cache_dict.items() if value[1] >= now
            }

    def get_nodes_for_key(
        self, key: str, nodes: Optional[List[str]] = None
    ) -> List[str]:
        nodes = nodes or self.get_live_nodes()
        ring = HashRing(nodes=nodes)
        primary_node = ring.get_node(key=key)

        sec_ring = HashRing(nodes=[node for node in nodes if node != primary_node])
        secondary_node = sec_ring.get_node(key=key)

        return [primary_node, secondary_node]

    def get_cache_value(
        self, key: str, local_only: Optional[bool] = False
    ) -> Union[Dict[str, Any], List, str, type(None)]:
        now = self.now()
        result = None
        local_value = self.cache_dict.get(key)
        if local_value is not None:
            if local_value[1] >= now:
                result = local_value[0]
        if local_only or result is not None:
            return result

        nodes = self.get_nodes_for_key(key=key)
        for node_ip in nodes:
            if node_ip == self.ip:
                continue
            result = self._get_remote_cache(key=key, ip=node_ip)
            if result is not None:
                break

        return result

    def set_cache_value(
        self,
        key: str,
        value: Union[Dict[str, Any], List, str],
        expiration_date: datetime.datetime,
        local_only: Optional[bool] = False,
    ):
        if local_only:
            self.cache_dict[key] = (value, expiration_date)
            return

        key_nodes = self.get_nodes_for_key(key)
        for node_ip in key_nodes:
            if node_ip == self.ip:
                self.cache_dict[key] = (value, expiration_date)
            elif node_ip is not None:
                self._set_remote_cache(
                    key=key, value=value, expiration_date=expiration_date, ip=node_ip
                )
        self.s3_client.put_object(
            Bucket=self.bucket, Key=key, Body=json.dumps(value), Expires=expiration_date
        )

    def refresh_cache(self):
        result = {}
        nodes = self.get_live_nodes()
        for key in self.get_all_persisted_keys():
            key_nodes = self.get_nodes_for_key(key=key, nodes=nodes)
            if self.ip in key_nodes:
                value = self.load_value_from_persistence(key=key)
                if value is not None:
                    result[key] = value
        self.cache_dict = result
        self.refresh_required = False

    def get_all_persisted_keys(self, max_keys=1000):
        response = self.s3_client.list_objects_v2(Bucket=self.bucket, MaxKeys=max_keys)
        if "Contents" not in response:
            return []
        for record in response["Contents"]:
            yield record["Key"]

        while response["IsTruncated"]:
            response = self.s3_client.list_objects_v2(
                Bucket=self.bucket,
                MaxKeys=max_keys,
                ContinuationToken=response["NextContinuationToken"],
            )
            for record in response["Contents"]:
                yield record["Key"]

    def load_value_from_persistence(
        self,
        key,
    ):
        result = None
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket,
                Key=key,
            )
            if response["Expires"] >= pytz.utc.localize(datetime.datetime.utcnow()):
                non_localized = datetime.datetime.fromisoformat(
                    response["Expires"].isoformat().split("+")[0]
                )
                result = json.loads(response["Body"].read()), non_localized
        except Exception as e:
            print(e)

        return result

    def _set_remote_cache(
        self,
        key,
        value,
        expiration_date,
        ip,
    ):
        response = requests.put(
            url=f"http://{ip}:{self.port}/internal/keys/{key}",
            json={"data": value, "expiration_date": expiration_date.isoformat()},
        )

    def _get_remote_cache(
        self,
        key,
        ip,
    ):
        result = None

        try:
            response = requests.get(url=f"http://{ip}:{self.port}/internal/keys/{key}")
            result = response.json() if response.ok else None
        except Exception as e:
            print(f"Caught exception {e}")

        return result

    def send_refresh_to_all_nodes(self):
        nodes = self.get_live_nodes()
        for node_ip in nodes:
            if node_ip != self.ip:
                requests.post(url=f"http://{node_ip}:{self.port}/internal/refresh")
