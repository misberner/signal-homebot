/** Loads libsignal's native library and generates a key pair, so an unsupported
 * CPU or missing native library fails loudly (at image build and in inspect mode). */
public class LibsignalCheck {
    public static void main(String[] args) {
        var pair = org.signal.libsignal.protocol.IdentityKeyPair.generate();
        System.out.println("libsignal OK (" + pair.getPublicKey().serialize().length + "-byte identity key)");
    }
}
