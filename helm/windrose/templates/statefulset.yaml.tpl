{{- include "windrose.validatePersistence" . -}}
{{- include "windrose.validateEnv" . -}}
{{- $persistence := include "windrose.persistenceSpec" . | fromYaml -}}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "windrose.fullname" . }}
  namespace: {{ .Values.namespace }}
  labels:
{{ include "windrose.labels" . | indent 4 }}
spec:
  serviceName: {{ include "windrose.fullname" . }}
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ include "windrose.name" . }}
      app.kubernetes.io/instance: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{ include "windrose.name" . }}
        app.kubernetes.io/instance: {{ .Release.Name }}
    spec:
      terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}
      # All containers in the pod share a PID namespace so the UI sidecar
      # can `pgrep` for the game's wine processes.
      shareProcessNamespace: true
      {{- if .Values.hostNetwork }}
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      {{- end }}
      {{- $extraAliases := list }}
      {{- range .Values.blackholeRegions }}
      {{- $extraAliases = append $extraAliases (dict "ip" "192.0.2.1" "hostnames" (list (printf "r5coopapigateway-%s-release.windrose.support" .))) }}
      {{- end }}
      {{- $allAliases := concat .Values.hostAliases $extraAliases }}
      {{- with $allAliases }}
      hostAliases:
{{ toYaml . | indent 8 }}
      {{- end }}
      securityContext:
{{ toYaml .Values.securityContext | indent 8 }}
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
{{ toYaml . | indent 8 }}
      {{- end }}
      {{- with .Values.nodeSelector }}
      nodeSelector:
{{ toYaml . | indent 8 }}
      {{- end }}
      containers:
        - name: windrose
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          securityContext:
{{ toYaml .Values.containerSecurityContext | indent 12 }}
          env:
            - name: WINDROSE_CONFIG_MODE
              value: {{ .Values.serverConfig.mode | quote }}
            - name: WINDROSE_LAUNCH_STRATEGY
              value: {{ .Values.serverConfig.launchStrategy | quote }}
            - name: SERVER_NAME
              value: {{ .Values.serverConfig.serverName | quote }}
            - name: INVITE_CODE
              value: {{ .Values.serverConfig.inviteCode | quote }}
            - name: IS_PASSWORD_PROTECTED
              value: {{ .Values.serverConfig.isPasswordProtected | quote }}
            - name: MAX_PLAYER_COUNT
              value: {{ .Values.serverConfig.maxPlayerCount | quote }}
            {{- if .Values.serverConfig.p2pProxyAddress }}
            - name: P2P_PROXY_ADDRESS
              value: {{ .Values.serverConfig.p2pProxyAddress | quote }}
            {{- end }}
            # Empty/unset p2pProxyAddress: the entrypoint auto-detects the
            # host's LAN-facing IP via a UDP-connect(8.8.8.8) + getsockname
            # trick. Under hostNetwork: true that's the interface the node
            # actually routes out of, which is what clients need as the ICE
            # host candidate. Downward API status.hostIP was tempting but it
            # returns whatever the kubelet registers as InternalIP — often
            # an overlay address that LAN clients can't reach.
            # --- Direct IP Connection ----------------------------------
            # Only pass through when useDirectConnection is explicitly
            # set (true|false). null leaves the field alone so existing
            # deployments aren't silently flipped.
            {{- if ne .Values.serverConfig.useDirectConnection nil }}
            - name: USE_DIRECT_CONNECTION
              value: {{ .Values.serverConfig.useDirectConnection | quote }}
            - name: DIRECT_CONNECTION_SERVER_ADDRESS
              value: {{ .Values.serverConfig.directConnection.serverAddress | quote }}
            - name: DIRECT_CONNECTION_SERVER_PORT
              value: {{ .Values.serverConfig.directConnection.serverPort | quote }}
            - name: DIRECT_CONNECTION_PROXY_ADDRESS
              value: {{ .Values.serverConfig.directConnection.proxyAddress | quote }}
            {{- end }}
            - name: WORLD_ISLAND_ID
              value: {{ .Values.worldConfig.islandId | quote }}
            - name: WORLD_NAME
              value: {{ .Values.worldConfig.name | quote }}
            - name: WORLD_PRESET_TYPE
              value: {{ .Values.worldConfig.presetType | quote }}
            - name: PROTON_USE_XALIA
              value: {{ .Values.protonUseXalia | quote }}
            - name: WINDROSE_PATCH_IDLE_CPU
              value: {{ .Values.patchIdleCpu | default 0 | quote }}
            - name: FILES_WAIT_TIMEOUT_SECONDS
              value: {{ .Values.filesWaitTimeoutSeconds | quote }}
            - name: SERVER_LAUNCH_ARGS
              value: {{ .Values.serverConfig.launchArgs | quote }}
            - name: NET_SERVER_MAX_TICK_RATE
              value: {{ .Values.serverConfig.networkTickRate | quote }}
            - name: WINDROSE_SERVER_SOURCE
              value: {{ .Values.serverConfig.source | default "steamcmd" | quote }}
            - name: DISPLAY
              value: ":{{ .Values.xvfb.display | default 99 }}"
            {{- with .Values.env }}
{{ toYaml . | indent 12 }}
            {{- end }}
            {{- if eq .Values.serverConfig.mode "managed" }}
            - name: WINDROSE_MANAGED_CONFIG_TEMPLATE
              value: "/etc/windrose/managed/ServerDescription.json"
            {{- if .Values.serverConfig.passwordSecret.name }}
            - name: WINDROSE_MANAGED_CONFIG_PASSWORD_FILE
              value: "/etc/windrose/secrets/password"
            {{- end }}
            {{- else if eq .Values.serverConfig.mode "mutable" }}
            - name: EXTERNAL_CONFIG
              value: "1"
            {{- else if eq .Values.serverConfig.mode "env" }}
            {{- if .Values.serverConfig.passwordSecret.name }}
            - name: SERVER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.serverConfig.passwordSecret.name }}
                  key: {{ .Values.serverConfig.passwordSecret.key | quote }}
            {{- end }}
            {{- else }}
            {{- fail (printf "unsupported serverConfig.mode %q (expected env, managed, or mutable)" .Values.serverConfig.mode) }}
            {{- end }}
          resources:
{{ toYaml .Values.resources.game | indent 12 }}
          volumeMounts:
{{ toYaml $persistence.mounts | indent 12 }}
            {{- if .Values.xvfb.enabled }}
            - name: x11-socket
              mountPath: /tmp/.X11-unix
            {{- end }}
            {{- if eq .Values.serverConfig.mode "managed" }}
            - name: managed-config-template
              mountPath: /etc/windrose/managed
              readOnly: true
            {{- if .Values.serverConfig.passwordSecret.name }}
            - name: managed-config-password
              mountPath: /etc/windrose/secrets
              readOnly: true
            {{- end }}
            {{- end }}
        {{- if .Values.xvfb.enabled }}
        - name: xvfb
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          # Wrap Xvfb in a trap-forwarding shell so kubelet's SIGTERM
          # reaches it on shutdown. AppArmor puts PID 1's original
          # binary on a stricter profile than the shell preStop spawns,
          # which causes `kill -TERM 1` from preStop to silently drop.
          # Keeping PID 1 as the shell sidesteps that.
          command:
            - sh
            - -c
            - |
              Xvfb :{{ .Values.xvfb.display | default 99 }} -screen 0 1024x768x24 -nolisten tcp &
              child=$!
              trap 'kill -TERM "$child" 2>/dev/null; wait "$child"' TERM INT
              wait "$child"
          resources:
{{ toYaml .Values.resources.xvfb | indent 12 }}
          securityContext:
{{ toYaml .Values.containerSecurityContext | indent 12 }}
          volumeMounts:
            - name: x11-socket
              mountPath: /tmp/.X11-unix
        {{- end }}
        - name: windrose-ui
          # Same image as the game container — consolidated from a
          # separate busybox+CGI image to a single Python admin-console
          # entrypoint baked in at /opt/windrose-ui/. Operator can
          # override with .Values.uiImage.* if they really want a
          # separate ui image, falls back to .Values.image.*.
          image: "{{ .Values.uiImage.repository | default .Values.image.repository }}:{{ .Values.uiImage.tag | default .Values.image.tag }}"
          imagePullPolicy: {{ .Values.uiImage.pullPolicy | default .Values.image.pullPolicy }}
          command: ["python3", "/opt/windrose-ui/server.py"]
          securityContext:
            allowPrivilegeEscalation: {{ .Values.containerSecurityContext.allowPrivilegeEscalation }}
            readOnlyRootFilesystem:   {{ .Values.containerSecurityContext.readOnlyRootFilesystem }}
            capabilities:
              drop:
                - ALL
              # Required so the UI can signal the game process in the
              # sibling container (POST /api/server/stop). Scoped to
              # this one container only.
              add:
                - KILL
          env:
            - name: UI_BIND
              value: "0.0.0.0"
            - name: UI_PORT
              value: {{ .Values.service.port | quote }}
            # HTTP basic auth. Empty = no auth (OK on LAN-only Ingress);
            # set to enable admin-protected access. Username is ignored.
            {{- if .Values.ui.passwordSecret.name }}
            - name: UI_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.ui.passwordSecret.name }}
                  key: {{ .Values.ui.passwordSecret.key | quote }}
            {{- else if .Values.ui.password }}
            - name: UI_PASSWORD
              value: {{ .Values.ui.password | quote }}
            {{- end }}
            - name: UI_ENABLE_ADMIN_WITHOUT_PASSWORD
              value: {{ .Values.ui.enableAdminWithoutPassword | quote }}
            - name: UI_SERVE_STATIC
              value: {{ .Values.ui.serveStatic | quote }}
            {{- with .Values.ui.webhooks }}
            - name: WINDROSE_WEBHOOK_EVENTS
              value: {{ .events | quote }}
            - name: WINDROSE_WEBHOOK_POLL_SECONDS
              value: {{ .pollSeconds | quote }}
            - name: WINDROSE_WEBHOOK_TIMEOUT
              value: {{ .timeout | quote }}
            {{- if .urlSecret.name }}
            - name: WINDROSE_WEBHOOK_URL
              valueFrom:
                secretKeyRef:
                  name: {{ .urlSecret.name }}
                  key: {{ .urlSecret.key | quote }}
            {{- else if .url }}
            - name: WINDROSE_WEBHOOK_URL
              value: {{ .url | quote }}
            {{- end }}
            {{- if .discordUrlSecret.name }}
            - name: WINDROSE_DISCORD_WEBHOOK_URL
              valueFrom:
                secretKeyRef:
                  name: {{ .discordUrlSecret.name }}
                  key: {{ .discordUrlSecret.key | quote }}
            {{- else if .discordUrl }}
            - name: WINDROSE_DISCORD_WEBHOOK_URL
              value: {{ .discordUrl | quote }}
            {{- end }}
            {{- end }}
            # Surfaced to the UI for CPU/mem %-of-cap display. Raw k8s
            # quantity strings ("500m", "2", "16Gi"). Empty falls through
            # to cgroup / host detection.
            - name: WINDROSE_GAME_CPU_LIMIT
              value: {{ .Values.resources.game.limits.cpu | default "" | quote }}
            - name: WINDROSE_GAME_MEM_LIMIT
              value: {{ .Values.resources.game.limits.memory | default "" | quote }}
            {{- with .Values.env }}
{{ toYaml . | indent 12 }}
            {{- end }}
          ports:
            - name: ui
              containerPort: {{ .Values.service.port }}
              protocol: TCP
          resources:
{{ toYaml .Values.resources.ui | indent 12 }}
          volumeMounts:
{{ toYaml $persistence.mounts | indent 12 }}
      volumes:
        {{- $root := . }}
        {{- range $persistence.claims }}
        - name: {{ .name }}
          persistentVolumeClaim:
            claimName: {{ include "windrose.persistenceClaimName" (dict "root" $root "claim" .) }}
        {{- end }}
        {{- if .Values.xvfb.enabled }}
        - name: x11-socket
          emptyDir: {}
        {{- end }}
        {{- if eq .Values.serverConfig.mode "managed" }}
        - name: managed-config-template
          configMap:
            name: {{ include "windrose.managedConfigTemplateName" . }}
        {{- if .Values.serverConfig.passwordSecret.name }}
        - name: managed-config-password
          secret:
            secretName: {{ .Values.serverConfig.passwordSecret.name }}
        {{- end }}
        {{- end }}
