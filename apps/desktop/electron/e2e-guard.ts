/**
 * FIRST import of the Electron main process under agent e2e.
 *
 * Registers uncaughtException / unhandledRejection handlers (log + exit 1)
 * before any other module — especially link-metadata — evaluates. Hidden
 * windows cannot hide Electron's native error dialog, so a crash must exit
 * instead of popping a box on the operator's desk.
 *
 * Keep this file free of other imports so it stays the earliest side-effect.
 */
import { installE2eCrashExit } from './e2e-hidden'

installE2eCrashExit()
