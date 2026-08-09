import 'package:flutter/material.dart';

import '../app_theme.dart';

class PrivacyScreen extends StatefulWidget {
  const PrivacyScreen({super.key, required this.onAccepted});
  final Future<void> Function() onAccepted;

  @override
  State<PrivacyScreen> createState() => _PrivacyScreenState();
}

class _PrivacyScreenState extends State<PrivacyScreen> {
  bool agreed = false;
  bool busy = false;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 520),
            child: ListView(
              padding: const EdgeInsets.fromLTRB(24, 42, 24, 24),
              children: [
                const _Brand(),
                const SizedBox(height: 36),
                Text('一起把每一段盲道\n变得更安全',
                    style: Theme.of(context).textTheme.headlineMedium?.copyWith(
                        fontWeight: FontWeight.w800,
                        height: 1.25,
                        color: AppTheme.ink)),
                const SizedBox(height: 12),
                Text('拍照上报、地图协作、公共派单，让复杂障碍也能被看见和解决。',
                    style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                        color: Colors.blueGrey.shade600, height: 1.6)),
                const SizedBox(height: 28),
                const _PermissionCard(
                    icon: Icons.photo_camera_outlined,
                    title: '相机与相册',
                    detail: '用于拍摄障碍物与任务完成凭证，只在您主动提交时上传。'),
                const _PermissionCard(
                    icon: Icons.location_on_outlined,
                    title: '定位与高德地图',
                    detail: '用于标注障碍位置、展示周边任务；同意前不会初始化地图或定位。'),
                const _PermissionCard(
                    icon: Icons.mail_outline,
                    title: '邮箱验证',
                    detail: '邮箱仅用于验证码登录和重要状态通知，不保存用户密码。'),
                const SizedBox(height: 18),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: agreed,
                  activeColor: AppTheme.teal,
                  onChanged: (value) => setState(() => agreed = value ?? false),
                  title: const Text('我已阅读并同意隐私与安全说明',
                      style:
                          TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
                  subtitle: const Text('危险施工、坑洼和固定设施请勿擅自处理。',
                      style: TextStyle(fontSize: 12)),
                  controlAffinity: ListTileControlAffinity.leading,
                ),
                const SizedBox(height: 8),
                FilledButton(
                  onPressed: !agreed || busy
                      ? null
                      : () async {
                          setState(() => busy = true);
                          await widget.onAccepted();
                        },
                  child: Text(busy ? '正在进入…' : '同意并继续'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _Brand extends StatelessWidget {
  const _Brand();
  @override
  Widget build(BuildContext context) => Row(children: [
        Container(
            width: 46,
            height: 46,
            decoration: BoxDecoration(
                color: AppTheme.teal, borderRadius: BorderRadius.circular(15)),
            child: const Icon(Icons.alt_route_rounded, color: Colors.white)),
        const SizedBox(width: 12),
        const Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('视桥',
              style: TextStyle(
                  fontSize: 21,
                  fontWeight: FontWeight.w800,
                  color: AppTheme.ink)),
          Text('VISIONBRIDGE VOLUNTEER',
              style: TextStyle(
                  fontSize: 9,
                  letterSpacing: 1.1,
                  color: AppTheme.teal,
                  fontWeight: FontWeight.w700))
        ])
      ]);
}

class _PermissionCard extends StatelessWidget {
  const _PermissionCard(
      {required this.icon, required this.title, required this.detail});
  final IconData icon;
  final String title;
  final String detail;
  @override
  Widget build(BuildContext context) => Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
          padding: const EdgeInsets.all(16),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                    color: AppTheme.teal.withValues(alpha: .08),
                    borderRadius: BorderRadius.circular(12)),
                child: Icon(icon, color: AppTheme.teal)),
            const SizedBox(width: 13),
            Expanded(
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                  Text(title,
                      style: const TextStyle(
                          fontWeight: FontWeight.w700, color: AppTheme.ink)),
                  const SizedBox(height: 5),
                  Text(detail,
                      style: TextStyle(
                          fontSize: 12,
                          height: 1.55,
                          color: Colors.blueGrey.shade600))
                ]))
          ])));
}
