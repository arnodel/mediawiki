# mediawiki

## Description

MediaWiki is a free wiki software application. Developed by the Wikimedia
Foundation and others, it is used to run all of the projects hosted by the
Foundation, including Wikipedia, Wiktionary and Commons. Numerous other wikis
around the world also use it to power their websites. It is written in the PHP
programming language and uses a backend database.

This charm will deploy MediaWiki in to the cloud while applying best practices
for running scale-out infrastructures, php based applications and MediaWiki. In
addition to the required minimum of MySQL -> MediaWiki; this charm also accepts
several other services to provide a more robust service experience.

## Usage

This charm is available in the Juju Charm Store along with hundreds of others.
To deploy this charm you will need: [a cloud environment][1], a working
[Juju][2] installation, and an already bootstrapped environment.

Once bootstrapped, deploy the [MySQL][3] and MediaWiki charm:

    juju deploy mysql
    juju deploy mediawiki

Add a relation between the two. Note: To avoid recieving "ambiguous relation"
error, specify the "db" relation:

    juju relate mysql mediawiki:db

Expose the MediaWiki service

    juju expose mediawiki

## Configuration

MediaWiki charm comes with a handful of settings designed to help streamline and
manage your deployment. For convenience if any applicable MediaWiki setting
variables are associated with the change they'll be listed in parentheses ().

### MediaWiki name ($wgSitename)

This will set the name of the Wiki installation.

    juju config mediawiki name='Juju Wiki!'

### Skin ($wgDefaultSkin)

As the option implies, this sets the default skin for all new users and anonymous users.

    juju config mediawiki skin='monobook'

One limitation is already registered users will have whatever Skin was set as
the default applied to their account. This is a [MediaWiki "limitation"][4]. See
caveats for more information on running Maintenance scripts.

### Admins

This will configure admin accounts for the MediaWiki instance. The expected format is user:pass

    juju set mediawiki admins="tom:swordfish"

This creates a user "tom" and sets their password to "swordfish". In the even
you wish to add more than one admin at a time you can provide a list of
user:pass values separated by a space " ":

    juju set mediawiki admins="tom:swordfish mike:wazowsk1"

This will create both of those users. At this time setting the admins option to
noting ("") will neither add or remove any existing admins. It's simply skipped.
To avoid having the password and usernames exposed consider running the
following after you've set up admin accounts:

    juju set mediawiki admins=""

## Debug ($wgDebugLogFile)

When set to true this option will enable the following MediaWiki options:
`$wgDebugLogFile`, `$wgDebugComments`, `$wgShowExceptionDetails`,
`$wgShowSQLErrors`, `$wgDebugDumpSql`, and `$wgShowDBErrorBacktrace`. A log file
will be crated in the charm's root directory on each machine called "debug.log".
For most providers this will be
`/var/lib/juju/units/mediawiki-0/charm/debug.log`, where `mediawiki-0` is the
name of the service and unit number.

## Relations

This charm requires a `db` relation providing the `mysql` interface (which can
be provided by the [MySQL][3] charm).  The charm will not be operational without it
as it uses mysql as its storage layer.  Se the Usage section above.

An optional caching layer is supported by requiring a `cache` relation providing
the `memcache` interface, which is provided by the [Memcached][5] charm.  To set it
up:

    juju deploy memcached
    juju relate memcached mediawiki

The above will only provide benefits if the mediawiki application has been
scaled out.

The charm also provides a `website` relation with the `http` interface, allowing
it to be connected to a load balancer such as [Haproxy][6].  This is particularly
useful when the `mediawiki` application is scaled out.  To set it up:

    juju deploy haproxy
    juju relate mediawiki:website haproxy:reverseproxy
    juju expose haproxy

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines 
on enhancements to this charm following best practice guidelines, and
`CONTRIBUTING.md` for developer guidance.

## MediaWiki Project Information

- [MediaWiki home page](http://www.mediawiki.org)
- [MediaWiki bug tracker](http://www.mediawiki.org/wiki/Bugzilla)
- [MediaWiki mailing lists](http://www.mediawiki.org/wiki/Mailing_lists)

[1]: https://juju.ubuntu.com/docs/getting-started.html
[2]: https://juju.ubuntu.com/docs/getting-started.html#installation
[3]: https://charmhub.io/mysql
[4]: http://www.mediawiki.org/wiki/Manual:$wgDefaultSkin
[5]: https://charmhub.io/memcached
[6]: https://charmhub.io/haproxy
