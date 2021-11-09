<?php

$wgSitename = "{{ wiki_name }}";

# Site language code, should be one of the list in ./languages/data/Names.php
$wgLanguageCode = "{{ language_code }}";

## Default skin: you can change the default skin. Use the internal symbolic
## names, ie 'vector', 'monobook':
$wgDefaultSkin = "{{ skin }}";

{% if logo_path %}
## The URL path to the logo.  Make sure you change this from the default,
## or else you'll overwrite your logo when you upgrade!
$wgLogo = "{{ logo_path }}";
{% endif %}

{% if server_address %}
$wgServer = "{{ server_address }}"
{% endif %}

{% if debug_file %}
$wgDebugLogFile = "{{ debug_file }}";
$wgDebugComments = true;
$wgShowExceptionDetails = true;
$wgShowSQLErrors = true;
$wgDebugDumpSql = true;
$wgShowDBErrorBacktrace = true;
{% endif %}
