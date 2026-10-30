//
//  Aster-Bridging-Header.h
//  Exposes the Aster C ABI to Swift.
//

#import "aster/aster.h"
#ifdef ASTER_SYSTEM_ENGINE
#import "aster/system_engine.h"
#endif
