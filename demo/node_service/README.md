# Node.js Demo Service: Inventory Service

This service fulfills requirement **[5b]**: proving Heimdall is truly OpenTelemetry-native and language-agnostic by ingesting traces generated from an Express app instrumented with the official OpenTelemetry Node.js SDK (`@opentelemetry/sdk-node`).

## Architecture
- **Language / Framework**: Node.js v18+ / Express.js
- **Port**: `8004`
- **Tracing**: Exports binary OTLP protobuf traces over HTTP directly to Heimdall (`POST http://localhost:4318/v1/traces`).
- **Service Name**: `inventory-service`

## Running Locally

1. Install dependencies:
   ```bash
   cd demo/node_service
   npm install
   ```

2. Start the service with tracing:
   ```bash
   npm start
   ```

3. Endpoints:
   - `GET http://localhost:8004/health`
   - `POST http://localhost:8004/reserve` (payload: `{"item_id": "widget-1", "quantity": 2, "order_id": "ord_101"}`)
   - `POST http://localhost:8004/fault` (payload: `{"latencyMs": 1500, "errorRate": 1.0}`)
   - `POST http://localhost:8004/fault/reset`
