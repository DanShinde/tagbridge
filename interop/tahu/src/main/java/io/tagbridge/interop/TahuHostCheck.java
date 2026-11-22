package io.tagbridge.interop;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.eclipse.tahu.message.SparkplugBPayloadDecoder;
import org.eclipse.tahu.message.model.Metric;
import org.eclipse.tahu.message.model.MetricDataType;
import org.eclipse.tahu.message.model.SparkplugBPayload;

/**
 * Acts as a passive Sparkplug B host application: subscribes to spBv1.0/#, decodes every payload
 * with the Eclipse Tahu reference decoder and checks session rules (seq ordering, bdSeq pairing,
 * births before data, aliases and datatypes declared in births).
 *
 * <p>Usage: TahuHostCheck [broker-url] [seconds] [min-messages]
 */
public final class TahuHostCheck {

    private final SparkplugBPayloadDecoder decoder = new SparkplugBPayloadDecoder();
    private final List<String> errors = new ArrayList<>();
    private final Map<String, Long> lastSeq = new HashMap<>();
    private final Map<String, Long> nodeBdSeq = new HashMap<>();
    private final Map<String, Map<Long, MetricDataType>> aliases = new HashMap<>();
    private final Map<String, Map<String, MetricDataType>> names = new HashMap<>();
    private final Map<String, Integer> counts = new HashMap<>();
    private int messages;

    public static void main(String[] args) throws Exception {
        String url = args.length > 0 ? args[0] : "tcp://localhost:1883";
        int seconds = args.length > 1 ? Integer.parseInt(args[1]) : 20;
        int minimum = args.length > 2 ? Integer.parseInt(args[2]) : 10;
        TahuHostCheck check = new TahuHostCheck();
        MqttClient client = new MqttClient(url, "tahu-host-check-" + System.nanoTime(),
                new MemoryPersistence());
        MqttConnectOptions options = new MqttConnectOptions();
        options.setCleanSession(true);
        client.connect(options);
        client.subscribe("spBv1.0/#", 1, (topic, message) -> check.onMessage(topic, message));
        System.out.println("listening on " + url + " for " + seconds + " s");
        TimeUnit.SECONDS.sleep(seconds);
        client.disconnect();
        client.close();
        System.exit(check.report(minimum) ? 0 : 1);
    }

    private synchronized void onMessage(String topic, MqttMessage message) {
        String[] parts = topic.split("/");
        if (parts.length < 4 || "STATE".equals(parts[1])) {
            return;
        }
        String type = parts[2];
        String node = parts[1] + "/" + parts[3];
        String device = parts.length > 4 ? node + "/" + parts[4] : node;
        messages++;
        counts.merge(type, 1, Integer::sum);
        SparkplugBPayload payload;
        try {
            payload = decoder.buildFromByteArray(message.getPayload(), null);
        } catch (Exception e) {
            error(topic, "Tahu could not decode payload: " + e);
            return;
        }
        if (type.endsWith("CMD")) {
            return;
        }
        if (payload.getTimestamp() == null) {
            error(topic, "payload has no timestamp");
        }
        if ("NDEATH".equals(type)) {
            Long bd = bdSeq(payload);
            if (bd == null) {
                error(topic, "NDEATH without bdSeq");
            } else if (nodeBdSeq.containsKey(node) && !bd.equals(nodeBdSeq.get(node))) {
                error(topic, "NDEATH bdSeq " + bd + " does not match NBIRTH bdSeq " + nodeBdSeq.get(node));
            }
            lastSeq.remove(node);
            return;
        }
        Long seq = payload.getSeq();
        if (seq == null) {
            error(topic, "missing seq");
            return;
        }
        if ("NBIRTH".equals(type)) {
            if (seq != 0) {
                error(topic, "NBIRTH seq must be 0, got " + seq);
            }
            Long bd = bdSeq(payload);
            if (bd == null) {
                error(topic, "NBIRTH without bdSeq");
            } else {
                nodeBdSeq.put(node, bd);
            }
            if (find(payload, "Node Control/Rebirth") == null) {
                error(topic, "NBIRTH without Node Control/Rebirth");
            }
        } else {
            Long previous = lastSeq.get(node);
            if (previous == null) {
                error(topic, type + " before NBIRTH");
            } else if (seq != (previous + 1) % 256) {
                error(topic, "seq " + seq + " after " + previous);
            }
        }
        lastSeq.put(node, seq);

        if ("DBIRTH".equals(type) || "NBIRTH".equals(type)) {
            Map<Long, MetricDataType> byAlias = new HashMap<>();
            Map<String, MetricDataType> byName = new HashMap<>();
            for (Metric metric : payload.getMetrics()) {
                if (!metric.hasName()) {
                    error(topic, "birth metric without a name");
                    continue;
                }
                if (metric.getDataType() == null || metric.getDataType() == MetricDataType.Unknown) {
                    error(topic, "birth metric " + metric.getName() + " without datatype");
                }
                if (byName.put(metric.getName(), metric.getDataType()) != null) {
                    error(topic, "duplicate metric name " + metric.getName());
                }
                if (metric.hasAlias() && byAlias.put(metric.getAlias(), metric.getDataType()) != null) {
                    error(topic, "duplicate alias " + metric.getAlias());
                }
            }
            aliases.put(device, byAlias);
            names.put(device, byName);
        } else if ("DDATA".equals(type) || "NDATA".equals(type)) {
            if (!names.containsKey(device)) {
                error(topic, type + " before DBIRTH");
                return;
            }
            for (Metric metric : payload.getMetrics()) {
                MetricDataType declared = metric.hasName()
                        ? names.get(device).get(metric.getName())
                        : aliases.get(device).get(metric.getAlias());
                if (declared == null) {
                    error(topic, "metric " + (metric.hasName() ? metric.getName() : "#" + metric.getAlias())
                            + " was not declared in the birth");
                } else if (metric.getDataType() != null && metric.getDataType() != declared) {
                    error(topic, "datatype " + metric.getDataType() + " differs from birth " + declared);
                }
            }
        } else if ("DDEATH".equals(type)) {
            names.remove(device);
            aliases.remove(device);
        }
    }

    private static Metric find(SparkplugBPayload payload, String name) {
        for (Metric metric : payload.getMetrics()) {
            if (metric.hasName() && name.equals(metric.getName())) {
                return metric;
            }
        }
        return null;
    }

    private static Long bdSeq(SparkplugBPayload payload) {
        Metric metric = find(payload, "bdSeq");
        return metric == null || metric.getValue() == null ? null : ((Number) metric.getValue()).longValue();
    }

    private void error(String topic, String text) {
        if (errors.size() < 50) {
            errors.add(topic + ": " + text);
        }
    }

    private boolean report(int minimum) {
        System.out.println("messages: " + messages + " " + counts);
        for (String error : errors) {
            System.out.println("FAIL " + error);
        }
        if (messages < minimum) {
            System.out.println("FAIL only " + messages + " messages, expected at least " + minimum);
            return false;
        }
        if (errors.isEmpty()) {
            System.out.println("OK: every payload decoded with Eclipse Tahu and followed the session rules");
        }
        return errors.isEmpty();
    }
}
