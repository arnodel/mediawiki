<?php
{% if servers %}
$wgMainCacheType = CACHE_MEMCACHED;
$wgMemCachedServers = [
{% for server in servers %}
    '{{ server.address }}:{{ server.port }}',
];
{% endfor %}
{% endif %}
