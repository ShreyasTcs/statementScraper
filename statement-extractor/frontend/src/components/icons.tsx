import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function Icon({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  )
}

export const CheckIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M3 8.5 6.2 11.7 13 4.9" />
  </Icon>
)

export const AlertIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 2.8 1.8 13.2h12.4L8 2.8Z" />
    <path d="M8 6.6v3" />
    <circle cx="8" cy="11.4" r="0.5" fill="currentColor" stroke="none" />
  </Icon>
)

export const InfoIcon = (props: IconProps) => (
  <Icon {...props}>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 7.2v4M8 4.9v.6" />
  </Icon>
)

export const LinkIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M6.6 9.4a2.6 2.6 0 0 0 3.8.2l1.9-1.9a2.6 2.6 0 0 0-3.7-3.7l-1 1" />
    <path d="M9.4 6.6a2.6 2.6 0 0 0-3.8-.2L3.7 8.3a2.6 2.6 0 0 0 3.7 3.7l1-1" />
  </Icon>
)

export const ChevronLeft = (props: IconProps) => (
  <Icon {...props}>
    <path d="M10 3.5 5.5 8l4.5 4.5" />
  </Icon>
)

export const ChevronRight = (props: IconProps) => (
  <Icon {...props}>
    <path d="M6 3.5 10.5 8 6 12.5" />
  </Icon>
)

export const CaretRight = (props: IconProps) => (
  <Icon {...props} strokeWidth={1.4}>
    <path d="M6.5 4 10 8l-3.5 4" />
  </Icon>
)

export const PlusIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 3.5v9M3.5 8h9" />
  </Icon>
)

export const MinusIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M3.5 8h9" />
  </Icon>
)

export const UploadCloudIcon = (props: IconProps) => (
  <Icon {...props} strokeWidth={1.5}>
    <path d="M4.6 12.5a3.1 3.1 0 0 1-.3-6.2 4 4 0 0 1 7.7-1 2.9 2.9 0 0 1 .3 5.7" />
    <path d="M8 13.4V7.2M5.9 9.2 8 7.1l2.1 2.1" />
  </Icon>
)

export const ArrowRightIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M3 8h10M9.2 4.2 13 8l-3.8 3.8" />
  </Icon>
)

export const SparkIcon = (props: IconProps) => (
  <Icon {...props} strokeWidth={1.4}>
    <path d="M8 2.2 9.3 6l3.8 1.3L9.3 8.6 8 12.4 6.7 8.6 2.9 7.3 6.7 6 8 2.2Z" />
  </Icon>
)

export const HomeIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M2.8 7 8 2.8 13.2 7v6.2H2.8V7Z" />
  </Icon>
)

export const ShieldIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M8 2.2 13 4v4.1c0 3-2.1 5-5 5.7-2.9-.7-5-2.7-5-5.7V4l5-1.8Z" />
  </Icon>
)

export const TargetIcon = (props: IconProps) => (
  <Icon {...props}>
    <circle cx="8" cy="8" r="5.4" />
    <circle cx="8" cy="8" r="2" />
  </Icon>
)

export const DocIcon = (props: IconProps) => (
  <Icon {...props}>
    <path d="M9.2 2.2H4.6v11.6h6.8V4.4L9.2 2.2Z" />
    <path d="M9.2 2.2v2.2h2.2" />
  </Icon>
)

/** File-type tile used on the upload screen and the processing header. */
export function FileTypeTile({ type, tone = 'red' }: { type: string; tone?: 'red' | 'slate' }) {
  const palette =
    tone === 'red' ? 'bg-[#FDECEA] text-[#B42318]' : 'bg-page text-ink-subtle border border-line'
  return (
    <span
      className={`inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-[10px] font-bold uppercase tracking-wide ${palette}`}
    >
      {type}
    </span>
  )
}
