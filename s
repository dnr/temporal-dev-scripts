#!/bin/sh
./temporal-server --allow-no-auth ${env:+--env $env} ${zone:+--zone $zone} "${@:-start}" | jlogs
