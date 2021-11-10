<?php

## Database settings
$wgDBtype = "mysql";
$wgDBserver = "{{ db["private-address"] }}";
$wgDBname = "{{ db["database"]}}";
$wgDBuser = "{{ db["user"] }}";
$wgDBpassword = "{{ db["password"] }}";

# MySQL specific settings
$wgDBprefix = "";

# MySQL table options to use during installation or update
$wgDBTableOptions = "ENGINE=InnoDB, DEFAULT CHARSET=binary";
