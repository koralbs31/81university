"use client"

import * as React from "react"
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "./dropdown-menu"
import { cn } from "@/lib/utils"

interface HoverMenuProps {
  children: React.ReactNode
  content: React.ReactNode
  contentClassName?: string
  align?: "start" | "center" | "end"
}

// Use the native menu primitive for keyboard, focus return and Escape handling.
// The historic name is retained for existing dashboard callers.
const HoverMenu = React.forwardRef<HTMLDivElement, HoverMenuProps>(
  ({ children, content, contentClassName, align = "start" }, ref) => (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>{children}</DropdownMenuTrigger>
      <DropdownMenuContent ref={ref} side="right" sideOffset={8} align={align} className={cn("border-white/10 bg-[#0f0f10] p-0 text-white", contentClassName)}>
        {content}
      </DropdownMenuContent>
    </DropdownMenu>
  )
)
HoverMenu.displayName = "HoverMenu"

interface HoverMenuContentProps {
  children: React.ReactNode
  className?: string
}

const HoverMenuContent = React.forwardRef<HTMLDivElement, HoverMenuContentProps>(
  ({ children, className }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "min-w-[200px] rounded-lg border bg-[#0f0f10] border-white/10 shadow-xl shadow-black/30 py-1",
          className
        )}
      >
        {children}
      </div>
    )
  }
)
HoverMenuContent.displayName = "HoverMenuContent"

interface HoverMenuItemProps {
  children: React.ReactNode
  className?: string
  onClick?: () => void
  asChild?: boolean
}

const HoverMenuItem = React.forwardRef<HTMLDivElement, HoverMenuItemProps>(
  ({ children, className, onClick, asChild }, ref) => (
    <DropdownMenuItem ref={ref} asChild={asChild} onSelect={onClick} className={cn("px-3 py-2 text-sm text-white/80 focus:bg-white/10 focus:text-white", className)}>
      {children}
    </DropdownMenuItem>
  )
)
HoverMenuItem.displayName = "HoverMenuItem"

interface HoverMenuLabelProps {
  children: React.ReactNode
  className?: string
}

const HoverMenuLabel = React.forwardRef<HTMLDivElement, HoverMenuLabelProps>(
  ({ children, className }, ref) => {
    return (
      <div
        ref={ref}
        className={cn(
          "px-3 py-2 text-xs font-medium text-white/75",
          className
        )}
      >
        {children}
      </div>
    )
  }
)
HoverMenuLabel.displayName = "HoverMenuLabel"

const HoverMenuSeparator = React.forwardRef<HTMLDivElement, { className?: string }>(
  ({ className }, ref) => {
    return (
      <div
        ref={ref}
        className={cn("my-1 h-px bg-white/10", className)}
      />
    )
  }
)
HoverMenuSeparator.displayName = "HoverMenuSeparator"

export {
  HoverMenu,
  HoverMenuContent,
  HoverMenuItem,
  HoverMenuLabel,
  HoverMenuSeparator,
}
