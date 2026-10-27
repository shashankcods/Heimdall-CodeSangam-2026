'use strict';

const express = require('express');
const opentelemetry = require('@opentelemetry/api');

const app = express();
const PORT = parseInt(process.env.PORT || '8004', 10);
const tracer = opentelemetry.trace.getTracer('inventory-service');

app.use(express.json());

let faultState = {
  latencyMs: 0,
  errorRate: 0.0,
  errorMessage: 'Warehouse reservation timeout',
};

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    service: 'inventory-service',
    runtime: `Node.js ${process.version}`,
    port: PORT,
  });
});

app.get('/fault', (req, res) => {
  res.json({ ...faultState, faultActive: faultState.latencyMs > 0 || faultState.errorRate > 0 });
});

app.post('/fault', (req, res) => {
  const { latencyMs = 0, errorRate = 0.0, errorMessage = 'Warehouse reservation timeout' } = req.body || {};
  faultState = { latencyMs, errorRate, errorMessage };
  console.log(`[inventory-service] Fault updated: latency=${latencyMs}ms, errorRate=${errorRate}`);
  res.json({ status: 'updated', fault: faultState });
});

app.post('/fault/reset', (req, res) => {
  faultState = { latencyMs: 0, errorRate: 0.0, errorMessage: 'Warehouse reservation timeout' };
  console.log('[inventory-service] Fault reset to normal');
  res.json({ status: 'reset', fault: faultState });
});

app.post('/reserve', async (req, res) => {
  const { item_id = 'item-101', quantity = 1, order_id = 'ord_demo' } = req.body || {};

  // Child internal span for inventory check
  await tracer.startActiveSpan('checkInventoryStock', async (span) => {
    span.setAttribute('inventory.item_id', item_id);
    span.setAttribute('inventory.quantity', quantity);
    span.setAttribute('inventory.order_id', order_id);

    // Apply latency fault if configured, or default 15ms lookup
    const delay = faultState.latencyMs > 0 ? faultState.latencyMs : 15;
    await new Promise((resolve) => setTimeout(resolve, delay));

    // Apply error fault
    if (faultState.errorRate > 0 && Math.random() < faultState.errorRate) {
      span.setStatus({
        code: opentelemetry.SpanStatusCode.ERROR,
        message: faultState.errorMessage,
      });
      span.end();
      return res.status(500).json({ error: faultState.errorMessage, order_id });
    }

    span.setStatus({ code: opentelemetry.SpanStatusCode.OK });
    span.end();

    res.json({
      status: 'reserved',
      item_id,
      quantity,
      order_id,
      reservation_id: `resv_${Math.random().toString(36).substring(2, 10)}`,
    });
  });
});

app.listen(PORT, () => {
  console.log(`[inventory-service] Node.js Express service running on port ${PORT}`);
});
