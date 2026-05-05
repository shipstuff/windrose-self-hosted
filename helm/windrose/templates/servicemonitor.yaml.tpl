{{- if and .Values.metrics.enabled .Values.metrics.serviceMonitor.enabled }}
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {{ include "windrose.fullname" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
{{- with .Values.metrics.serviceMonitor.labels }}
{{ toYaml . | indent 4 }}
{{- end }}
{{- with .Values.metrics.serviceMonitor.annotations }}
  annotations:
{{ toYaml . | indent 4 }}
{{- end }}
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ include "windrose.name" . }}
      app.kubernetes.io/instance: {{ .Release.Name }}
  endpoints:
    - port: metrics
      path: /metrics
      interval: {{ .Values.metrics.serviceMonitor.interval | quote }}
      scrapeTimeout: {{ .Values.metrics.serviceMonitor.scrapeTimeout | quote }}
{{- end }}
