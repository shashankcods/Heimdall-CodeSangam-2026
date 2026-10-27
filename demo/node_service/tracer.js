'use strict';

const opentelemetry = require('@opentelemetry/sdk-node');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { Resource } = require('@opentelemetry/resources');
const { ATTR_SERVICE_NAME } = require('@opentelemetry/semantic-conventions');

const HEIMDALL_URL = process.env.HEIMDALL_URL || 'http://localhost:4318';

const traceExporter = new OTLPTraceExporter({
  url: `${HEIMDALL_URL}/v1/traces`,
});

const sdk = new opentelemetry.NodeSDK({
  resource: new Resource({
    [ATTR_SERVICE_NAME || 'service.name']: 'inventory-service',
    'service.version': '1.0.0',
    'environment': 'demo',
  }),
  traceExporter,
  instrumentations: [
    getNodeAutoInstrumentations({
      '@opentelemetry/instrumentation-fs': { enabled: false },
    }),
  ],
});

sdk.start();
console.log(`[inventory-service] OTel Node SDK initialized -> ${HEIMDALL_URL}/v1/traces`);

process.on('SIGTERM', () => {
  sdk.shutdown()
    .then(() => console.log('Tracing terminated'))
    .catch((err) => console.log('Error terminating tracing', err))
    .finally(() => process.exit(0));
});
