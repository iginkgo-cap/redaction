/**
 * Central icon catalog for compliance-tools.
 *
 * All icons are re-exported from `lucide-react` under the app's semantic names,
 * replacing the previously hand-written inline SVGs. lucide is tree-shakeable
 * (only imported icons are bundled, ~1kB each) and carries no antd dependency.
 *
 * Conventions — every icon is a React component that accepts:
 *   - `size`        px size (number), applied to both width and height (default 24)
 *   - `strokeWidth` stroke thickness (default 2; use ~1.6 for large decorative icons)
 *   - `className`   for styling / colour (icons stroke with `currentColor`)
 * All share viewBox `0 0 24 24`, `stroke="currentColor"`, round line caps/joins.
 *
 * Prefer importing from this module over importing from `lucide-react` directly,
 * so the app keeps a single icon vocabulary.
 */
export {
  Plus as PlusIcon,
  History as HistoryIcon,
  Upload as UploadIcon,
  WandSparkles as AutoFillIcon,
  Trash2 as DeleteIcon,
  RotateCw as ReloadIcon,
  Inbox as InboxIcon,
  ImageUp as ImageUpIcon,
  Pencil as PencilIcon,
  // 窄屏折叠后的 section 栏把手（LOOK-5755）：一份目录, 所以是 List 不是汉堡 Menu ——
  // 汉堡在本仓和别处都读作「整个应用的导航」。
  List as ListIcon,
  // 字段备注（LOOK-5755）：评论气泡，不是铅笔。铅笔在本仓已经是「改这个东西本身」的意思
  // （改名、改模板），而备注是**挂在字段旁边的一句话**，不改答案。
  MessageSquare as NoteIcon,
  // 审核意见另起别名: MessageSquare 已被 NoteIcon 占着, 复用会让读代码的人
  // 在审核意见里看到 NoteIcon(LOOK-6521)。
  MessageSquarePlus as ReviewCommentIcon,
  // Signature-request actions (LOOK-5933): a nib stroke, distinct from the Pencil used for rename.
  PenLine as PenLineIcon,
  Signature as SignatureIcon,
  Calendar as DateIcon,
  X as CloseIcon,
  Check as CheckIcon,
  ChevronRight as ChevronRightIcon,
  ChevronDown as ChevronDownIcon,
  ChevronLeft as ChevronLeftIcon,
  ChevronUp as ChevronUpIcon,
  Search as SearchIcon,
  Folder as FolderIcon,
  FileText as FileTextIcon,
  // Home-grid feature tiles. CtcPdfHelperIcon shares the FileText glyph; kept as
  // a distinct semantic name so the home grid reads clearly at the call site.
  FileText as CtcPdfHelperIcon,
  FileInput as QuickFormIcon,
  // PDF Masking home tile (LOOK-5885).
  Eraser as PdfMaskIcon,
  ClipboardCheck as KycFormIcon,
  // A Copy filled against an earlier form (LOOK-6023). A clock carrying an alert mark: the
  // state is "out of date", not generic trouble, and the generic warning triangle says nothing
  // about time.
  ClockAlert as OutdatedIcon,
  // 字段列的显示开关（LOOK-6073）：一个左侧竖栏的面板图形，说的是「那一列在不在」。
  PanelLeft as FieldsPanelIcon,
  // 填表助手的附件按钮（LOOK-6073）。
  Paperclip as PaperclipIcon,
  // 助手消息里的一次工具调用（LOOK-6073）：一个小扳手比转圈更能说明「它在动手做事」。
  Wrench as ToolActivityIcon,
  // 往会话里挂客户资料（LOOK-6262）：与回形针并列的第二个「加素材」入口。用 Users 而不是
  // Building/Folder —— 挂进来的是**人与主体**（自然人、信托、公司），而不是文件或机构。
  Users as ClientRecordsIcon,
  // 一次工具调用**没有回来**（LOOK-6262）：流在它在飞时断了，我们不知道它成没成。
  // 用带斜杠的圆而不是三角警告：这不是「出错了」，是「没有结果」——三角会把一个未知说成失败。
  CircleSlash as NoResultIcon,
} from 'lucide-react';

export type { LucideProps as IconProps } from 'lucide-react';
