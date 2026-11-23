// Independent original Go/C provider used only for port comparisons.
package main

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"os"
)

const (
	mlDSA44PublicKeySize  = 1312
	mlDSA44PrivateKeySize = 2560
	mlDSA44SignatureSize  = 2420
)

var ErrVerifierUnavailable = errors.New("ML-DSA-44 verifier unavailable")

type Verifier struct {
	verify func([]byte, []byte, []byte) bool
}

func Verifier_New(verify func([]byte, []byte, []byte) bool) *Verifier {
	return &Verifier{verify: verify}
}

type request struct {
	Operation  string
	PublicKey  []byte
	PrivateKey []byte
	Message    []byte
	Signature  []byte
}

type response struct {
	Available    bool
	Verified     bool
	Signature    []byte
	Message      []byte `json:",omitempty"`
	PublicKey    []byte
	PrivateKey   []byte
	Error        string
	ErrorIsExact bool
}

func main() {
	var inputs []request
	if err := json.NewDecoder(os.Stdin).Decode(&inputs); err != nil {
		panic(err)
	}
	outputs := make([]response, len(inputs))
	for index, input := range inputs {
		output := &outputs[index]
		var err error
		switch input.Operation {
		case "new":
			var verifier *Verifier
			verifier, err = NewVerifier()
			output.Available = verifier != nil && verifier.verify != nil
		case "verify":
			var verifier *Verifier
			verifier, err = NewVerifier()
			if err == nil {
				output.Verified = verifier.verify(input.PublicKey, input.Message, input.Signature)
			}
		case "sign":
			output.Signature, err = signWithPrivateKey(input.Message, input.PrivateKey)
		case "keypair":
			output.PublicKey, output.PrivateKey, err = generateMLDSA44Keypair()
		case "device-keypair":
			output.PublicKey, output.PrivateKey, err = ed25519.GenerateKey(nil)
		case "device-sign":
			if len(input.PrivateKey) != ed25519.PrivateKeySize {
				err = errors.New("invalid fixture device key")
			} else {
				output.Signature = ed25519.Sign(input.PrivateKey, input.Message)
			}
		case "device-verify":
			if len(input.PublicKey) != ed25519.PublicKeySize {
				err = errors.New("invalid fixture device public key")
			} else {
				output.Verified = ed25519.Verify(input.PublicKey, input.Message, input.Signature)
			}
		case "app-manifest-sign":
			output.Message, err = json.Marshal(json.RawMessage(input.Message))
			if err == nil && len(input.PrivateKey) == ed25519.PrivateKeySize {
				output.Signature = ed25519.Sign(input.PrivateKey, append([]byte("daochi-app-manifest-v1\n"), output.Message...))
			} else if err == nil {
				err = errors.New("invalid fixture device key")
			}
		default:
			panic("unknown provider operation")
		}
		if err != nil {
			output.Error = err.Error()
			output.ErrorIsExact = err == ErrVerifierUnavailable
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(outputs); err != nil {
		panic(err)
	}
}
