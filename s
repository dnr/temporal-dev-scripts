#!/bin/sh
: ${env:=development-sqlite-file}
./temporal-server --allow-no-auth ${env:+--env $env} ${zone:+--zone $zone} "${@:-start}" | jlogs
