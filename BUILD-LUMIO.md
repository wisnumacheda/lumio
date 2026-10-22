# Resep Build APK Lumio (fork Tatap) — terbukti sukses 22 Okt 2026

Hasil: `Lumio-v2.11.0.apk` (24.453.573 byte, SHA-256 8170f69869a853bf06eab0b46985b24a8b19267f4d8178b70d5c5711df6b305c),
package `id.tatap.fork`, label "Lumio", v2-signature terverifikasi (sertifikat fork CN=Tatap Fork), arm64-v8a, minSdk 24.

## Kenapa build di sandbox Muse butuh trik khusus
1. Sandbox memblokir koneksi TCP IPv4 dari proses JVM (daemon Gradle mati). **IPv6 loopback lolos** →
   paksa semua JVM (client + daemon + worker) dengan `JAVA_TOOL_OPTIONS=-Djava.net.preferIPv6Addresses=true`.
2. JVM membaca user.home dari passwd (=/root), bukan $HOME → set `GRADLE_USER_HOME=/home/hatch/.gradle`
   (di situlah gradle.properties berisi konfigurasi proxy egress).
3. Proxy egress memakai CA sendiri → import sekali ke cacerts JDK:
   `keytool -importcert -noprompt -trustcacerts -alias hatch-egress-ca -file /usr/local/share/ca-certificates/hatch-egress-ca.crt -keystore ~/workspace/tools/jdk17/lib/security/cacerts -storepass changeit`
4. Izin Muse: Settings → Permissions → Direct network protocols → other_tcp = **Allow** (Ask tidak cukup).

## Perintah build
```bash
cd ~/workspace/tatap-fork/android
export JAVA_HOME=~/workspace/tools/jdk17 ANDROID_HOME=~/workspace/tools/sdk GRADLE_USER_HOME=/home/hatch/.gradle
export KEYSTORE_FILE=keystore/release.jks KEY_ALIAS=tatap_release
export KEYSTORE_PASSWORD=$(grep '^StorePass:' ~/workspace/tatap-fork-signing-info.txt | cut -d: -f2 | tr -d ' \r')
export KEY_PASSWORD=$(grep '^KeyPass' ~/workspace/tatap-fork-signing-info.txt | cut -d: -f2 | tr -d ' \r')
export JAVA_TOOL_OPTIONS="-Djava.net.preferIPv6Addresses=true"
export GRADLE_OPTS="-Djava.net.preferIPv6Addresses=true"
export PATH=~/workspace/tools/py311/bin:$JAVA_HOME/bin:$PATH
~/workspace/tools/gradle-8.5/bin/gradle assembleMobileRelease
# output: android/app/build/outputs/apk/mobile/release/tatap-release-v2.11.0.apk
```

Sebelum build ulang: sinkronkan source root ke tree Android bila backend/frontend berubah
(`cp -r backend/. android/app/src/main/python/backend/` dan `cp -r frontend android/app/src/main/python/frontend`).
Kata sandi keystore hanya ada di ~/workspace/tatap-fork-signing-info.txt — jangan tulis di sini.
