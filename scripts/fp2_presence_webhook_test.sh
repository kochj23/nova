#!/bin/bash
# Test script: simulate all 4 FP2 sensors reporting
# Replace with actual HomeKit automation URLs

ENDPOINT="http://192.168.1.6:8089/presence"

# Simulate: office occupied, others empty
curl -s -X POST "$ENDPOINT" -H "Content-Type: application/json" -d '{"room":"office","presence":true,"source":"homekit"}'
curl -s -X POST "$ENDPOINT" -H "Content-Type: application/json" -d '{"room":"living_room","presence":false,"source":"homekit"}'
curl -s -X POST "$ENDPOINT" -H "Content-Type: application/json" -d '{"room":"master_bedroom","presence":false,"source":"homekit"}'
curl -s -X POST "$ENDPOINT" -H "Content-Type: application/json" -d '{"room":"patio","presence":false,"source":"homekit"}'

echo "Done - check http://192.168.1.6:8089/health"
