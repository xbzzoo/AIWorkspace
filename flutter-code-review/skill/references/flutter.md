# Flutter 平台规则包

> 配合 `flutter-code-review` 主 SKILL.md 使用。

## D4 · 编程规范 Checklist（Flutter）

> Flutter 工程的"怎么干最标准"完整清单，9 组覆盖架构 / Widget / 性能 / Dart / 错误处理 / 三方库 / 测试 / 代码健康 / 其他平台规范。

### 架构与状态管理

- [ ] UI/业务逻辑分层清晰（UI 层只负责展示和触发事件，逻辑在 ViewModel / Controller / Bloc 中）
- [ ] 无滥用全局状态；复杂局部状态不全用 `setState`
- [ ] 严格遵守项目状态管理框架规范（Provider / Riverpod / Bloc 等）
- [ ] 状态变更限定 rebuild 最小范围 Widget（`Selector` / 细粒度 `Consumer`，非根节点 `setState`）

### Widget 设计与 UI 最佳实践

- [ ] Widget 树变长时提取为**独立 `StatelessWidget` 类**（**非** `_buildXxx` 方法）
- [ ] 优先 `StatelessWidget`；能用状态管理注入状态则不用 `StatefulWidget`
- [ ] 使用 `fliggy_design` 色值 / 字体；实现深色模式
- [ ] 处理 `SafeArea`；使用 `Flexible` / `Expanded` / `LayoutBuilder` 避免 `RenderFlex overflowed`

### 性能优化

- [ ] 不变 Widget 树和变量前加 `const`（综合考量，并非每处不加就算违规）
- [ ] 超过一屏列表使用 `ListView.builder` 或 `SliverList`
- [ ] `build()` 方法中无耗时操作（复杂循环、I/O）
- [ ] 频繁动画包裹 `RepaintBoundary`
- [ ] 图片通过 `FRoundImage` 相关 API 展示
- [ ] 避免在 `forEach` 流程中使用 `async`/`await`
- [ ] 避免 `GlobalKey` 跨组件传递

### Dart 语言特性与代码规范

- [ ] 无滥用 `!` 强制解包；`late` 使用安全；用 `?.` 和 `??` 处理空值
- [ ] 成员变量尽量 `final`；数据模型不可变，通过 `copyWith` 更新
- [ ] `async`/`await` 正确使用；异步异常有 `try-catch` 捕获
- [ ] 命名规范：文件 `snake_case`，类 `PascalCase`，变量/方法 `camelCase`
- [ ] 充分考虑边界异常
- [ ] `flutter analyze` 无 error

### 错误处理与健壮性

- [ ] 数据页面处理 **Loading / Empty / Error / 重试** 四个状态
- [ ] `await` 后更新 UI 前检查 `if (!mounted) return;`
- [ ] `AnimationController` / `ScrollController` / `TextEditingController` / `Timer` / `StreamSubscription` 在 `dispose()` 中释放

### State / Widget 生命周期接缝（高频漏报区，逐项核对）

> 4 个回调 `initState` / `didUpdateWidget` / `didChangeDependencies` / `dispose` 必须 "职责对账"，这是 Flutter 经典 bug 集中地。

- [ ] **prop 派生状态**：凡在 `initState` 中读 `widget.X` 算出的字段（list / map / controller / 索引 / 标志位），若 `widget.X` 可能在父级 rebuild 时变化，必须在 `didUpdateWidget` 中重新派生；否则 prop 翻转后 State 内的派生数据 stale，访问对应索引/字段会越界或读到旧值。
- [ ] **InheritedWidget 派生状态**：从 `Provider.of` / `context.dependOnInheritedWidgetOfExactType` 拿到的值，如果在 `initState` 用，必须改到 `didChangeDependencies`，并在依赖变化时同步内部状态。
- [ ] **Controller / Notifier 释放后再用**：`super.dispose()` 必须在最后一行；释放 `ValueNotifier` / `Controller` 后是否还有待执行的 `Timer` / `Future` 回调访问这些已释放对象？
- [ ] **Singleton / 静态 ValueNotifier 跨 State 复用**：`SearchTipsOverlay` 这类被 page state 持有又内部带计时器的对象，**第二次 `showOverlay` 前**是否显式重置内部状态（remainingTime / step / flag）？把"第一次能跑通"当作"永远能跑通"是典型陷阱。
- [ ] **build 中改 State / 跑副作用**：`build()` 内禁止 `setState` / 埋点 expose / 创建 controller / 注册监听 / 调用网络。曝光埋点放 `build()` 会在每次 rebuild 重复触发污染 A/B 数据。

### Widget 重配置反模式速查（反方视角）

> Review 任何 `StatefulWidget` 时，按下表逐格画 ✓/✗：

| 维度 | 检查 |
|------|------|
| `initState` 用了 `widget.X` | `didUpdateWidget` 是否同步处理 X 变化？ |
| `initState` 用了 `context` (InheritedWidget) | 是否搬到 `didChangeDependencies`？ |
| 字段持有 `Controller` / `Notifier` / `Subscription` / `Timer` | `dispose` 是否成对释放？`super.dispose()` 是否最后？ |
| 字段持有 `OverlayEntry` 或挂在全局单例 | 页面销毁时是否清理？ |
| 字段是 `late` 非空但赋值在 `initState` 之后 | 提前访问会 `LateInitializationError`，是否有路径绕过初始化？ |
| getter 内 `??` 兜底 `new XxxController()` | 每次访问都新建实例，丧失协同/状态语义 |
- [ ] `FBroadcast` 相关遵循规范（注册 / 反注册严格配对）
- [ ] `catch` 中异常通过 `FItrace` 相关 API 上报
- [ ] 禁止使用 `rootBundle` 相关 API
- [ ] 使用 asset 时有 `package` 参数（`Image.asset()` / `FRoundImage.asset()`）
- [ ] 检测到当前类是 model 角色时，建议添加 `copyWith`（提升代码质量，非硬性错误）

### 第三方库与依赖管理

- [ ] `MethodChannel` 调用处理 `MissingPluginException`
- [ ] 依赖稳定，无为小功能引入庞大库
- [ ] 杜绝分支依赖，使用 **tag 依赖**

### 测试

- [ ] 核心业务逻辑有单元测试
- [ ] 复杂自定义 Widget 有 Widget Tests

### 代码健康度

- [ ] 文档完整
- [ ] 注释清晰完整
- [ ] 无废代码 / 无用代码

### 其他

- [ ] 满足 Dart SDK `>=2.12.0 <3.0.0` API 规范
- [ ] `FliggyNavigatorApi` 跳转含 `flutter_view` 时有 `params: {'_fli_inner_router': 'android,ios'}`
- [ ] 本地缓存优先使用 `FliggyKV`
- [ ] 生命周期相关逻辑完全准确（`onResume` / `onPause` / `dispose` 配对，含 `FliggyPageMixin`）
