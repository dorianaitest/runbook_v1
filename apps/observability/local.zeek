@load policy/tuning/json-logs.zeek
redef Log::default_rotation_interval = 1 hr;

# --- Container attribution for DSGVO egress observability ---
# See specs/zeek-container-attribution.md. container-map.tsv is
# regenerated every few minutes by generate-container-map.sh (cron) and
# picked up automatically here via Input::REREAD — no Zeek restart needed.

type ContainerMapIdx: record {
	ip: addr;
};

type ContainerMapVal: record {
	container_name: string;
};

global container_map: table[addr] of ContainerMapVal = table();

event zeek_init()
	{
	Input::add_table([$source="/opt/zeek/share/zeek/site/container-map.tsv",
	                   $name="container_map",
	                   $idx=ContainerMapIdx,
	                   $val=ContainerMapVal,
	                   $destination=container_map,
	                   $mode=Input::REREAD]);
	}

redef record Conn::Info += {
	container_name: string &log &optional;
};

redef record DNS::Info += {
	container_name: string &log &optional;
};

hook Log::log_stream_policy(rec: Conn::Info, id: Log::ID)
	{
	if ( id != Conn::LOG )
		return;
	if ( rec$id$orig_h in container_map )
		rec$container_name = container_map[rec$id$orig_h]$container_name;
	}

hook Log::log_stream_policy(rec: DNS::Info, id: Log::ID)
	{
	if ( id != DNS::LOG )
		return;
	if ( rec$id$orig_h in container_map )
		rec$container_name = container_map[rec$id$orig_h]$container_name;
	}
