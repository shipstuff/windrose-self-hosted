apiVersion: v1
kind: Service
metadata:
  name: {{ include "windrose.fullname" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
{{- with .Values.metrics.serviceAnnotations }}
  annotations:
{{- range $key, $value := . }}
    {{ $key }}: {{ $value | quote }}
{{- end }}
{{- end }}
spec:
  type: {{ .Values.service.type }}
  selector:
    app.kubernetes.io/name: {{ include "windrose.name" . }}
    app.kubernetes.io/instance: {{ .Release.Name }}
  {{- if .Values.service.publishNotReadyAddresses }}
  publishNotReadyAddresses: true
  {{- end }}
  ports:
    - name: ui
      protocol: TCP
      port: {{ .Values.service.port }}
      targetPort: ui
    {{- if .Values.metrics.enabled }}
    - name: metrics
      protocol: TCP
      port: {{ .Values.metrics.port }}
      targetPort: metrics
    {{- end }}
