#!/bin/sh
./temporal-server ${env:+--env $env} ${zone:+--zone $zone} "${@:-start}" | jlogs
