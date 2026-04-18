{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "windrose.fullname" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
  {{- with .Values.ingress.annotations }}
  annotations:
{{ toYaml . | indent 4 }}
  {{- end }}
spec:
  {{- if .Values.ingress.className }}
  ingressClassName: {{ .Values.ingress.className }}
  {{- end }}
  rules:
    {{- range .Values.ingress.hosts }}
    - host: {{ . | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "windrose.fullname" $ }}
                port:
                  number: {{ $.Values.service.port }}
    {{- end }}
  {{- with .Values.ingress.tls }}
  tls:
{{ toYaml . | indent 4 }}
  {{- end }}
{{- end }}
