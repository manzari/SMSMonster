#!/usr/bin/with-contenv bashio
# Map add-on options to SMSMONSTER_* environment variables and start the gateway.
export SMSMONSTER_DATA_DIR=/data
export SMSMONSTER_PORT=8080
export SMSMONSTER_HOST=0.0.0.0
export SMSMONSTER_DEVICES="$(bashio::config 'devices')"
export SMSMONSTER_BAUDRATES="$(bashio::config 'baudrates')"
export SMSMONSTER_LOG_LEVEL="$(bashio::config 'log_level')"
export SMSMONSTER_HANGUP_CALLS="$(bashio::config 'hangup_calls')"
export SMSMONSTER_DELIVERY_REPORTS="$(bashio::config 'delivery_reports')"
export SMSMONSTER_HA_INTEGRATION="$(bashio::config 'ha_integration')"
if bashio::config.has_value 'sim_pin'; then export SMSMONSTER_SIM_PIN="$(bashio::config 'sim_pin')"; fi
if bashio::config.has_value 'api_token'; then export SMSMONSTER_API_TOKEN="$(bashio::config 'api_token')"; fi
export SMSMONSTER_TRUST_INGRESS=true
bashio::log.info "Starting SMS Monster (devices: ${SMSMONSTER_DEVICES})"
exec smsmonster
